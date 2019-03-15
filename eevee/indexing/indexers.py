#!/usr/bin/env python
# encoding: utf-8

import itertools
import multiprocessing
from collections import Counter, defaultdict
from datetime import datetime
from threading import Thread

from blinker import Signal
from elasticsearch_dsl import Search

from eevee.indexing.utils import DOC_TYPE, get_elasticsearch_client, update_refresh_interval

try:
    from queue import Empty
except ImportError:
    from Queue import Empty


class IndexingException(Exception):
    """
    Exception thrown when indexing fails for any reason.
    """

    def __init__(self, error_string):
        super(IndexingException, self).__init__(error_string)


class ElasticsearchBulkIndexingException(Exception):
    """
    Exception used to indicate that there was a probem when sending the data in bulk to
    elasticsearch.
    """

    def __init__(self, elasticsearch_response):
        super(ElasticsearchBulkIndexingException, self).__init__(u'Elasticsearch indexing failed')
        self.elasticsearch_response = elasticsearch_response


class IndexingProcess(multiprocessing.Process):
    """
    Process that indexes mongo documents placed on its queue.
    """

    def __init__(self, process_id, config, index, document_queue, result_queue, is_clean_insert,
                 elasticsearch_bulk_size, error_queue, stats_queue=None):
        """
        :param process_id: an identifier for this process (we'll include this when posting results
                           back)
        :param config: the config object
        :param index: the index object - this is used to get the commands and then identifies which
                      elasticsearch index to send them to
        :param document_queue: the queue of document objects we'll read from
        :param result_queue: the queue we'll write results to
        :param is_clean_insert: True if the index we're indexing into is empty, this allows us to
                                skip some checks and makes processing faster
        :param elasticsearch_bulk_size: the number of index requests to send in each bulk request
        :param error_queue: the queue we'll write errors to
        :param stats_queue: the queue we'll write created/updated stats to (default: None, means
                            don't send stats)
        """
        super(IndexingProcess, self).__init__()
        # not the actual OS level PID just an internal id for this process
        self.process_id = process_id
        self.config = config
        self.index = index
        self.document_queue = document_queue
        self.result_queue = result_queue
        self.is_clean_insert = is_clean_insert
        self.elasticsearch_bulk_size = elasticsearch_bulk_size
        self.error_queue = error_queue
        self.stats_queue = stats_queue

        # keep track of how many commands this process has sent to elasticsearch
        self.command_count = 0
        # keep track of the versions of the records we've handled in this process
        self.versions = set()
        self.stats = defaultdict(Counter)
        self.elasticsearch = get_elasticsearch_client(config, sniff_on_start=True,
                                                      sniff_on_connection_fail=True,
                                                      sniffer_timeout=60, sniff_timeout=10,
                                                      http_compress=False)

    def run(self):
        """
        Run the processing loop which reads from the document queue and sends indexing commands to
        elasticsearch.
        """
        # buffers for commands and ids we're going to modify
        command_buffer = []
        id_buffer = {}

        try:
            # do a blocking read from the queue until we get a sentinel
            for mongo_doc in iter(self.document_queue.get, None):
                # create the commands for the record
                commands = list(self.index.get_commands(mongo_doc))
                if commands:
                    # add all the versions seen in the commands
                    self.versions.update(
                        index_doc[u'meta'][u'version'] for _, index_doc in commands)
                    # then add them to the buffer
                    command_buffer.extend(commands)
                    # and add the id to the buffer (as an integer) along with the latest version of
                    # the record as it will be once indexed by elasticsearch
                    id_buffer[int(mongo_doc[u'id'])] = commands[-1][1]

                # send the commands to elasticsearch if the bulk size limit has been reached or
                # exceeded
                if len(command_buffer) >= self.elasticsearch_bulk_size:
                    # send the commands
                    self.send_to_elasticsearch(command_buffer, id_buffer)
                    # reset the buffers
                    command_buffer = []
                    id_buffer = {}

            if command_buffer:
                # if there are any commands left, handle them
                self.send_to_elasticsearch(command_buffer, id_buffer)

            # post the results back to the main process
            self.result_queue.put((self.process_id, self.command_count, self.stats, self.versions))
        except KeyboardInterrupt:
            # if we get a keyboard interrupt just stop
            pass
        except Exception as e:
            # use repr so we get the class too
            self.error_queue.put(repr(e))

    def send_to_elasticsearch(self, commands, ids):
        """
        Send the given commands to elasticsearch.

        :param commands: the commands to send, each element in the list should be a command pair -
                         the action and then the data (see the elasticsearch bulk api doc for
                         details)
        :param ids: the ids of the documents being updated and their latest data state
        """
        self.command_count += len(commands)

        # collect up some stats!
        created, updated = self.collect_stats(ids)

        # we can skip the delete step if there isn't any data in the index
        if not self.is_clean_insert:
            # delete any existing records with the ids we're about to update
            Search(index=self.index.name, using=self.elasticsearch)\
                .filter(u'terms', **{u'data._id': list(ids.keys())})\
                .delete()

        # send the commands to elasticsearch
        response = self.elasticsearch.bulk(itertools.chain.from_iterable(commands))

        # if there are errors in the response, raise an exception
        if response[u'errors']:
            raise ElasticsearchBulkIndexingException(response)

        # extract stats from the elasticsearch response
        for action_response in response[u'items']:
            # each item in the items list is a dict with a single key and value, we're interested in
            # the value
            info = next(iter(action_response.values()))
            # update the stats
            self.stats[info[u'_index']][info[u'result']] += 1

        # drop our stats on to the stats queue if necessary
        if self.stats_queue is not None:
            self.stats_queue.put((self.index.unprefixed_name, created, updated, ids))

    def collect_stats(self, ids):
        """
        Given a list of ids, figure out which ones will be created for the first time when indexed
        and which ones will be updated as they already exist in the index.

        If the stats_queue attribute on this object is None then two empty lists will be returned
        from this function and no stats will be collected.

        :param ids: a dict of integer record ids and their latest data states
        :return: a 2-tuple consisting of a list of created ids and a list of updated ids
        """
        # if there's no stats queue for stats to be sent back on then we shouldn't collect them up
        if self.stats_queue is None:
            return [], []

        all_ids = list(ids.keys())
        if self.is_clean_insert:
            # well that was easy, everything is a create when the index was empty beforehand
            return all_ids, []
        else:
            # find which record ids already exist in the index
            search = Search(index=self.index.name, using=self.elasticsearch)\
                .filter(u'terms', **{u'data._id': all_ids})\
                .source([u'data._id'])\
                .extra(size=len(ids))
            # extract the updated ids from the return
            updated = [hit[u'data'][u'_id'] for hit in search.execute()]
            # diff the lists to figure out which ones were created ones
            return list(set(all_ids) - set(updated)), updated


class Indexer(object):
    """
    Class encapsulating the functionality required to index records.
    """

    def __init__(self, version, config, feeders_and_indexes, queue_size=16000, pool_size=3,
                 elasticsearch_bulk_size=1000, update_status=True, signal_stats=True):
        """
        :param version: the version we're indexing up to
        :param config: the config object
        :param feeders_and_indexes: sequence of 2-tuples where each tuple is made up of a feeder
                                    object which provides the documents from mongo to index and an
                                    index object which will be used to generate the data to index
                                    from the feeder's documents
        :param queue_size: the maximum size of the document indexing process queue (default: 16000)
        :param pool_size: the size of the pool of processes to use (default: 3)
        :param elasticsearch_bulk_size: the number of index requests to send in each bulk request
        :param update_status: whether to update the status index after completing the indexing
                              (default: True)
        :param signal_stats: whether to collect stats about the indexing and fire the created and
                             updated signals (default: True)
        """
        self.version = version
        self.config = config
        self.feeders_and_indexes = feeders_and_indexes
        self.feeders, self.indexes = zip(*feeders_and_indexes)
        self.queue_size = queue_size
        self.pool_size = pool_size
        self.elasticsearch_bulk_size = elasticsearch_bulk_size
        self.update_status = update_status
        self.signal_stats = signal_stats
        self.elasticsearch = get_elasticsearch_client(self.config, sniff_on_start=True,
                                                      sniff_on_connection_fail=True,
                                                      sniffer_timeout=60, sniff_timeout=10,
                                                      http_compress=False)

        # setup the signals
        self.index_signal = Signal(doc=u'''Triggered when a record is about to be queued for index.
                                           Note that the document may or not be indexed after this
                                           signal is triggered, that is dependant on the index
                                           object and it's command creating logic. The kwargs passed
                                           when this signal is sent are "mongo_doc", "feeder",
                                           "index", "document_count" and "document_total" which hold
                                           the document being processed, the feeder object, the
                                           index object, the number of documents that have been
                                           handled so far and the total number of documents to be
                                           handled overall respectively.''')
        self.finish_signal = Signal(doc=u'''Triggered when the processing is complete. The kwargs
                                            passed when this signal is sent are "document_count",
                                            "command_count" and "stats", which hold the number of
                                            records that have been handled, the total number of
                                            index commands created from those records and the report
                                            stats that will be entered into mongo respectively.''')
        self.created_signal = Signal(doc=u'''Triggered after a record is actually indexed and it is
                                             the first time it's been indexed. The kwargs passed
                                             when this signal is sent are "index", "record_id" and
                                             "record" which hold the name of the index (unprefixed)
                                             into which the record was indexed, the integer record
                                             ID of the record and the latest record data
                                             respectively.''')
        self.updated_signal = Signal(doc=u'''Triggered after a record is actually indexed and it is
                                             the not time it's been indexed. The kwargs passed
                                             when this signal is sent are "index", "record_id" and
                                             "record" which hold the name of the index (unprefixed)
                                             into which the record was indexed, the integer record
                                             ID of the record and the latest record data
                                             respectively.''')
        self.start = datetime.now()

    def get_stats(self, operations, seen_versions):
        """
        Returns the statistics of a completed indexing in the form of a dict. The operations
        parameter is expected to be a dict of the form {index_name -> {<op>: #, ...}} but can take
        any form as long as it can be handled sensibly by any downstream functions.

        :param operations: a dict describing the operations that occurred
        :param seen_versions: the versions seen during indexing
        """
        end = datetime.now()
        # generate and return the report dict
        return {
            u'version': self.version,
            u'versions': sorted(seen_versions),
            u'sources': sorted(set(feeder.mongo_collection for feeder in self.feeders)),
            u'targets': sorted(set(index.name for index in self.indexes)),
            u'start': self.start,
            u'end': end,
            u'duration': (end - self.start).total_seconds(),
            u'operations': operations,
        }

    def stats_collector(self, stats_queue):
        """
        Should be run in a separate thread from the main index function as it uses a blocking get
        on the stats_queue parameter passed in.

        :param stats_queue: the stats queue to read from
        """
        try:
            for index, created, updated, data in iter(stats_queue.get, None):
                for record_id in created:
                    self.created_signal.send(self, index=index, record_id=record_id,
                                             record=data[record_id])
                for record_id in updated:
                    self.updated_signal.send(self, index=index, record_id=record_id,
                                             record=data[record_id])
        except KeyboardInterrupt:
            # if we're keyboard interrupted, just end
            pass

    def check_errors(self, error_queue, document_queue):
        """
        Given the error queue and the document queue, check if there are any errors. If there are
        we consume the document queue and then add the sentinels. Finall we raise and
        IndexingException.

        :param error_queue: the queue errors are put on
        :param document_queue: the queue documents to index are put on
        """
        if not error_queue.empty():
            try:
                # get the first error, if there is one
                error_string = error_queue.get_nowait()
                # consume the queue
                while not document_queue.empty():
                    document_queue.get()
                # put the sentinels on the queue
                for i in range(self.pool_size):
                    document_queue.put(None)
                # raise the problem
                raise IndexingException(error_string)
            except Empty:
                # if the queue is empty it's no big deal, just means no errors!
                pass

    def index(self):
        """
        Indexes a set of records from mongo into elasticsearch.
        """
        # define the mappings first
        self.define_indexes()

        # count how many documents from mongo have been handled
        document_count = 0
        # count how many index commands have been sent to elasticsearch
        command_count = 0
        # total stats across all feeder/index combinations
        op_stats = defaultdict(Counter)
        # seen versions
        seen_versions = set()
        # total up the number of documents to be handled by this indexer
        document_total = sum(feeder.total() for feeder in self.feeders)

        # check if there is any data already in the indexes targeted or not, this allows us to skip
        # some checks and makes the process faster
        clean_indexes = {
            index: Search(index=index.name, using=self.elasticsearch).count() == 0
            for index in set(self.indexes)
        }

        if self.signal_stats:
            # create a queue for per record created and updated stats to flow through and a thread
            # to pick them up and send them to the signal listeners
            stats_queue = multiprocessing.Queue(maxsize=10)
            stats_thread = Thread(target=self.stats_collector, args=(stats_queue, ))
            stats_thread.start()
        else:
            stats_queue = None
            stats_thread = None

        try:
            for feeder, index in self.feeders_and_indexes:
                # create a queue for documents
                document_queue = multiprocessing.Queue(maxsize=self.queue_size)
                # create a queue allowing results to be passed back once a process has completed
                result_queue = multiprocessing.Queue()
                # create a queue allowing errors to be passed back from the indexing subprocesses
                error_queue = multiprocessing.Queue()

                # create all the sub-processes for indexing and start them up
                process_pool = []
                for number in range(self.pool_size):
                    process = IndexingProcess(number, self.config, index, document_queue,
                                              result_queue, clean_indexes[index],
                                              self.elasticsearch_bulk_size, error_queue,
                                              stats_queue)
                    process_pool.append(process)
                    process.start()

                try:
                    if clean_indexes[index]:
                        # if we're doing a clean index, set the interval to -1 for performance
                        update_refresh_interval(self.elasticsearch, [index], -1)
                    else:
                        # we're just doing updates on an existing index set the interval to 30 secs
                        update_refresh_interval(self.elasticsearch, [index], u'30s')

                    # loop through the documents from the feeder
                    for mongo_doc in feeder.documents():
                        # check the error queue first
                        self.check_errors(error_queue, document_queue)

                        # do a blocking put onto the queue
                        document_queue.put(mongo_doc)
                        document_count += 1
                        self.index_signal.send(self, mongo_doc=mongo_doc, feeder=feeder,
                                               index=index, document_count=document_count,
                                               command_count=command_count,
                                               document_total=document_total)

                    # send a sentinel to each worker to indicate that we're done putting documents
                    # on the queue
                    for i in range(self.pool_size):
                        document_queue.put(None)

                    # if there are any processes still running, loop until they are complete (when
                    # they complete their slot in the process_pool list is replaced with None
                    while any(process_pool):
                        # check the errors first
                        self.check_errors(error_queue, document_queue)
                        try:
                            # retrieve some results from the result queue (block for 3 secs)
                            number, commands_handled, stats, versions = result_queue.get(timeout=3)
                        except Empty:
                            # if there were no completed processes yet, just loop round. This allows
                            # us to respond if error are found
                            continue
                        # set the process to None to signal that this process has completed
                        process_pool[number] = None
                        # add the stats for this process to our various counters
                        command_count += commands_handled
                        # update the global seen versions
                        seen_versions.update(versions)
                        # update the global stats counter
                        for index_name, counter in stats.items():
                            op_stats[index_name].update(counter)
                finally:
                    # set the refresh interval back to the default
                    update_refresh_interval(self.elasticsearch, [index], None)
        finally:
            if self.signal_stats:
                # everything is complete now, put a sentinel on the stats queue and wait for it to
                # finish up
                stats_queue.put(None)
                stats_thread.join()

        # update the status index
        self.update_statuses()
        # generate the stats dict
        stats = self.get_stats(op_stats, seen_versions)
        # trigger the finish signal
        self.finish_signal.send(self, document_count=document_count, command_count=command_count,
                                stats=stats)
        return stats

    def define_indexes(self):
        """
        Run through the indexes, ensuring they exist and creating them if they don't. Elasticsearch
        does create indexes automatically when they are first used but we want to set a custom
        mapping so we need to manually create them first.
        """
        # use a set to ensure we don't try to create an index multiple times
        for index in set(self.indexes):
            if not self.elasticsearch.indices.exists(index.name):
                self.elasticsearch.indices.create(index.name, body=index.get_index_create_body())

    def update_statuses(self):
        """
        Run through the indexes and update the statuses for each.
        """
        index_definition = {
            u'settings': {
                u'index': {
                    # this will always be a small index so no need to create a bunch of shards
                    u'number_of_shards': 1,
                    u'number_of_replicas': 1
                }
            },
            u'mappings': {
                DOC_TYPE: {
                    u'properties': {
                        u'name': {
                            u'type': u'keyword'
                        },
                        u'index_name': {
                            u'type': u'keyword'
                        },
                        u'latest_version': {
                            u'type': u'date',
                            u'format': u'epoch_millis'
                        }
                    }
                }
            }
        }
        # ensure the status index exists with the correct mapping
        if not self.elasticsearch.indices.exists(self.config.elasticsearch_status_index_name):
            self.elasticsearch.indices.create(self.config.elasticsearch_status_index_name,
                                              body=index_definition)

        if self.update_status:
            # use a set to avoid updating the status for an index multiple times
            for index in set(self.indexes):
                status_doc = {
                    u'name': index.unprefixed_name,
                    u'index_name': index.name,
                    u'latest_version': self.version,
                }
                self.elasticsearch.index(self.config.elasticsearch_status_index_name, DOC_TYPE,
                                         status_doc, id=index.name)
