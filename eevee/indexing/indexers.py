#!/usr/bin/env python3
# encoding: utf-8
import copy
import itertools
from collections import Counter, defaultdict
from datetime import datetime

import dictdiffer

from eevee import utils
from eevee.indexing import elasticsearch
from eevee.indexing.utils import DataToIndex, get_versions_and_data
from eevee.mongo import get_mongo


class Indexer:
    """
    Class encapsulating the functionality required to index records.
    """

    def __init__(self, config, feeder, indexes, mongo_chunk_size=1000):
        """
        :param config: the config object
        :param feeder: feeder object which provides the documents from mongo to inxed
        :param indexes: the indexes that the mongo collection will be indexed into
        :param mongo_chunk_size: the number of documents to retrieve per chunk
        """
        self.config = config
        self.feeder = feeder
        self.indexes = indexes
        self.mongo_chunk_size = mongo_chunk_size

        self.monitors = []
        self.start = datetime.now()

    def register_monitor(self, monitor_function):
        """
        Register a monitoring function with the indexer which receive updates after each chunk is indexed. The function
        should take a single parameter, a percentage complete so far represented as a decimal value between 0 and 1.

        :param monitor_function: the function to be called during indexing with details for monitoring
        """
        self.monitors.append(monitor_function)

    def report_stats(self, operations, latest_version):
        """
        Records statistics about the indexing run into the mongo index stats collection.

        :param operations: a dict describing the operations that occurred
        :param latest_version: the latest version that we have no indexed up until
        """
        end = datetime.now()
        stats = {
            'latest_version': latest_version,
            'source': self.feeder.mongo_collection,
            'start': self.start,
            'end': end,
            'duration': (end - self.start).total_seconds(),
            'operations': operations
        }
        with get_mongo(self.config, collection=self.config.mongo_indexing_stats_collection) as mongo:
            mongo.insert_one(stats)
        return stats

    def send_to_elasticsearch(self, data_to_index_chunk, stats):
        """
        Uses the indexes to convert the passed chunk into commands which will be sent to elasticsearch. The passed stats
        object is updated with the results.

        :param data_to_index_chunk: the chunk of data to index in elasticsearch
        :param stats: the stats object, which by default is a defaultdict of counters
        """
        # create all the commands necessary to index the data
        commands = []
        for index in self.indexes:
            # get the commands from the index
            commands.extend(itertools.chain.from_iterable(index.get_bulk_commands(data_to_index_chunk)))

        for response in elasticsearch.send_bulk_index(self.config, commands):
            # extract stats from the elasticsearch response
            for action_response in response.json()['items']:
                # each item in the items list is a dict with a single key and value, we're interested in the value
                info = next(iter(action_response.values()))
                # update the stats
                stats[info['_index']][info['result']] += 1

    def index(self):
        """
        Indexes a set of records from mongo into elasticsearch.
        """
        # define the mappings first
        self.define_mappings()

        # keep a record of the latest version seen as this will be used to update the current version alias
        latest_version = None
        # store for stats about the indexing operations that occur on each index
        stats = defaultdict(Counter)

        # work out the total number of documents we're going to go through and index, for monitoring purposes
        total_records_to_index = self.feeder.total()
        # keep a count of the number of documents indexed so far
        total_indexed_so_far = 0

        # loop over all the documents returned by the feeder
        for chunk in utils.chunk_iterator(self.feeder.documents(), chunk_size=self.mongo_chunk_size):
            data_to_index_chunk = []

            for mongo_doc in chunk:
                total_indexed_so_far += 1
                # wrap the mongo doc in a useful container object
                data_to_index = DataToIndex(mongo_doc)
                versions = mongo_doc['versions']

                # update the latest version
                latest_version_in_chunk = max(versions)
                if not latest_version or latest_version < latest_version_in_chunk:
                    latest_version = latest_version_in_chunk

                if not versions:
                    # TODO: sort out "versionless" data
                    data_to_index.add(mongo_doc['data'])
                else:
                    # add all the versions and data to data_to_index
                    for version, data in get_versions_and_data(mongo_doc):
                        data_to_index.add(data, version)

                data_to_index_chunk.append(data_to_index)

            # send the data to elasticsearch for indexing
            self.send_to_elasticsearch(data_to_index_chunk, stats)
            # update the monitoring functions with progress
            for monitor in self.monitors:
                monitor(total_indexed_so_far / total_records_to_index)

        # update the aliases
        self.update_aliases(latest_version)
        # report the statistics of the indexing operation back into mongo
        return self.report_stats(stats, latest_version)

    def define_mappings(self):
        """
        Run through the indexes and retrieve their mappings then send them to elasticsearch.
        """
        for index in self.indexes:
            elasticsearch.send_mapping(self.config, index.name, index.get_mapping())

    def update_aliases(self, latest_version):
        """
        Run through the indexes and retrieve the alias operations then send them to elasticsearch.

        :param latest_version: the latest version from the data indexed
        """
        for index in self.indexes:
            elasticsearch.send_aliases(self.config, index.get_alias_operations(latest_version))
