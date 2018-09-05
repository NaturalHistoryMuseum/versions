from eevee.indexing.utils import get_versions_and_data, DOC_TYPE
from eevee.utils import iter_pairs


class Index:
    """
    Represents an index in elasticsearch.
    """

    def __init__(self, config, name, version):
        """
        :param config: the config object
        :param name: the elasticsearch index name that the data held in this object will be indexed into, note that this
                     name will be prefixed with the config.elasticsearch_index_prefix value and stored in the name
                     attribute whereas the name without the prefix will be stored in the unprefixed_name attribute
        :param version: the version we're indexing up to
        """
        self.config = config
        self.unprefixed_name = name
        self.name = f'{config.elasticsearch_index_prefix}{name}'
        self.version = version

    def get_commands(self, mongo_doc):
        """
        Yields all the action and data dicts as a tuple for the given mongo doc.

        :param mongo_doc: the mongo doc to handle
        """
        # iterate over the data in pairs so that we can retrieve the next version too, use (None, None) as the final
        # pair's partner so that we can use unpacking
        for (version, data), (next_version, _next_data) in iter_pairs(get_versions_and_data(mongo_doc), (None, None)):
            yield self.create_action(mongo_doc['id'], version), self.create_index_document(data, version, next_version)

    def create_action(self, record_id, version):
        """
        Creates a dictionary containing the action information for elasticsearch. This tells elasticsearch what to do,
        i.e. index, delete, create etc.

        :param record_id: the id of the record
        :param version: the version of the record
        :return: a dictionary
        """
        # build and return the dictionary. Note that the document type is fixed as _doc as this parameter is no longer
        # used and will be removed in future versions of elasticsearch
        return {
            'index': {
                # create an id for the document which is unique by using the record id and the version
                '_id': f'{record_id}:{version}',
                '_type': DOC_TYPE,
                '_index': self.name,
            }
        }

    def create_index_document(self, data, version, next_version):
        """
        Creates the index dictionary for elasticsearch. This contains the actual data to be indexed.

        :param data: the data dict
        :param version: the version of the data
        :param next_version: the next version of the data which this data is correct until
        :return: a dictionary
        """
        return {
            'data': self.create_data(data),
            'meta': self.create_metadata(version, next_version),
        }

    def create_data(self, data):
        """
        Returns the data to be indexed in elasticsearch.

        :param data: the data dict to index
        :return: a dictionary of the actual data that will be indexed in elasticsearch
        """
        return data

    def create_metadata(self, version, next_version):
        """
        Returns a dictionary of metadata to be stored in elasticsearch along with the data.

        :param version: the version of the data
        :param next_version: the next version of the data
        :return: a dictionary of metadata information
        """
        metadata = {
            'versions': {
                'gte': version,
            },
            'version': version,
        }
        if next_version:
            metadata['versions']['lt'] = next_version
            metadata['next_version'] = next_version
        return metadata

    def get_index_create_body(self):
        """
        Returns a dict which will be passed to elasticsearch when the index is initialised.

        :return: a dict
        """
        return {
            'settings': {
                "analysis": {
                    "normalizer": {
                        "lowercase_normalizer": {
                            "type": "custom",
                            "char_filter": [],
                            "filter": ["lowercase"]
                        }
                    }
                }
            },
            # TODO: handle geolocations
            'mappings': {
                DOC_TYPE: {
                    'properties': {
                        'meta.versions': {
                            'type': 'date_range',
                            'format': 'epoch_millis'
                        },
                        'meta.version': {
                            'type': 'date',
                            'format': 'epoch_millis'
                        },
                        'meta.next_version': {
                            'type': 'date',
                            'format': 'epoch_millis'
                        },
                        # the values of each field will be copied into this field easy querying
                        "meta.all": {
                            "type": "text"
                        }
                    },
                    'dynamic_templates': [
                        {
                            # for all fields we want to:
                            #  - store them as a text type so that we can do free searches on them
                            #  - store them as a keyword type so that we can do keyword searches on them (available at
                            #    <field_name>.keyword)
                            #  - copy them to the meta.all field so that we can do queries across all fields easily
                            # this dynamic mapping accomplishes these three things
                            "standard_field": {
                                "path_match": "data.*",
                                "mapping": {
                                    "type": "text",
                                    "fields": {
                                        # index a keyword version of the field at <field_name>.keyword
                                        "keyword": {
                                            "type": "keyword",
                                            # ensure it's indexed lowercase so that it's easier to search
                                            "normalizer": "lowercase_normalizer",
                                            # 256 is the standard limit in elasticsearch
                                            "ignore_above": 256
                                        }
                                    },
                                    "copy_to": "meta.all",
                                }
                            }
                        }
                    ]
                }
            }
        }
