#!/usr/bin/env python
import errno
from math import floor, ceil
from errno import ENOENT, EDEADLOCK
import os
import signal
import subprocess
import time
from fuse import FUSE, FuseOSError, Operations, LoggingMixIn, fuse_get_context

from pymongo.errors import PyMongoError
from pymongo import MongoClient
import gridfs
from src.core.Configuration import Configuration
from pymongo.collection import ReturnDocument

from functools import wraps

"""
    Custom decorator to easily handle a MongoDB disconnection.
    We can (should) even use the wrapper in the connect / load method 
"""
def retry_connection(view_func):
    # It can be difficult to kill the mount because of fusepy. Ideally we would need the latest version of fusepy but they
    # didn't make a release for quite some times.
    def custom_kill(config):

        command = '/usr/bin/fusermount -u ' + str(config.mounting_point) + ''
        print('Try to umount the current file system: ' + str(command))
        # Run in background
        # TODO: It seems it never works to umount correctly with python directly. But if we enter the same command ourselves, or during
        # the startup, it works...
        subprocess.run([command], shell=True, stdout=subprocess.PIPE)

        # We wait a bit, as we hope the umount will work.
        time.sleep(15)

        # In that case, the mount point will be "corrupted", and only fixed once we try to mount again.
        print('It seems the umount did not work, kill the process itself.')

        SIGKILL = 9
        os.kill(os.getpid(), SIGKILL)


    def _decorator(*args, **kwargs):
        new_connection_attempt = False
        st = time.time()
        mongo_cache = args[0]
        while True:
            try:
                if new_connection_attempt is True:
                    time.sleep(0.5)
                    mongo_cache.connect()
                    mongo_cache.load_internal()

                response = view_func(*args, **kwargs)
                return response
            except PyMongoError as e:
                dt = time.time() - st
                if dt >= mongo_cache.configuration.mongo_access_attempt():
                    print('Problem to execute the query, maybe we are disconnected from MongoDB. ' +
                          'Max access attempt exceeded ('+str(int(dt))+'s >= '+str(mongo_cache.configuration.mongo_access_attempt())+'). ' +
                          'Stop the mount.')
                    custom_kill(mongo_cache.configuration)
                    # We want to exit the current loop
                    exit(1)
                else:
                    print('Problem to execute the query, maybe we are disconnected from MongoDB. Connect and try again.')
                    new_connection_attempt = True

    return wraps(view_func)(_decorator)

"""
    This class will implement every method that we need to connect to MongoDB, and every query should be run through it (with the exceptions of tests). 
    This is also an easy to handle the disconnection to MongoDB during a short amount of time.
"""
class MongoCache:
    instance = None
    configuration = None

    def __init__(self):
        # We reuse the same connexion
        if MongoCache.instance is None:
            MongoCache.configuration = Configuration()
            retry_connection(self.connect())
        retry_connection(self.load_internal())

    """
        Establish a connection to mongodb
    """
    def connect(self):
        mongo_path = 'mongodb://' + ','.join(MongoCache.configuration.mongo_hosts())
        MongoCache.instance = MongoClient(mongo_path)


    """
        Get the objects to connect to the correct database and collections
    """
    def load_internal(self):
        self.instance = MongoCache.instance
        self.database = MongoCache.instance[MongoCache.configuration.mongo_database()]

        # We use gridfs only to store the files. Even if we have a lot of small files, the overhead should
        # still be small.
        # Documentation: https://api.mongodb.com/python/current/api/gridfs/index.html
        gridfs_collection = MongoCache.configuration.mongo_prefix() + 'files'
        self.gridfs = gridfs.GridFS(self.database, gridfs_collection)

    """
        Simply retrieve any document
    """
    @retry_connection
    def find_one(self, coll, query):
        return self.database[coll].find_one(query)

    """
        A generic find function, which might be problematic to handle if we get a connection error while iterating on it.
        It needs to be handle on the caller side to avoid any problem.
    """
    @retry_connection
    def find(self, coll, query, projection=None):
        return self.database[coll].find(query, projection, no_cursor_timeout=True)

    """
        A FindOneAndUpdate which always return the document after modification
    """
    @retry_connection
    def find_one_and_update(self, coll, query, update):
        return self.database[coll].find_one_and_update(query, update, return_document=ReturnDocument.AFTER)

    """
        A simple insert_one
    """
    @retry_connection
    def insert_one(self, coll, document):
        return self.database[coll].insert_one(document)

    """ 
        A simple delete_many
    """
    @retry_connection
    def delete_many(self, coll, query):
        return self.database[coll].delete_many(query)

    """
        Create a new file with gridfs directly. Save it directly
    """
    @retry_connection
    def gridfs_new_file(self, file):
        f = self.gridfs.new_file(**file)
        f.close()

    """
        Delete a file in gridfs
    """
    @retry_connection
    def gridfs_delete(self, _id):
        return self.gridfs.delete(_id)

    """
        The drop command is only used for development normally
    """
    @retry_connection
    def drop(self, coll):
        return self.database[coll].drop()