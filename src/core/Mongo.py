#!/usr/bin/env python
from pymongo import MongoClient
import gridfs
import errno
from fuse import FUSE, FuseOSError, Operations, LoggingMixIn

from core.Configuration import Configuration
from core.GenericFile import GenericFile
from core.File import File
from core.Directory import Directory
from core.SymbolicLink import SymbolicLink
from math import floor, ceil

class Mongo:
    instance = None
    configuration = None

    def __init__(self):
        # We reuse the same connexion
        if Mongo.instance is None:
            Mongo.configuration = Configuration()
            self.connect()
        self.instance = Mongo.instance
        self.database = Mongo.instance[Mongo.configuration.mongo_database()]

        # We use gridfs only to store the files. Even if we have a lot of small files, the overhead should
        # still be small.
        # Documentation: https://api.mongodb.com/python/current/api/gridfs/index.html
        self.gridfs_collection = Mongo.configuration.mongo_prefix() + 'files'
        self.gridfs = gridfs.GridFS(self.database, self.gridfs_collection)

        self.files_coll =  self.database[Mongo.configuration.mongo_prefix() + 'files.files']
        self.chunks_coll =  self.database[Mongo.configuration.mongo_prefix() + 'files.chunks']

    """
        Load the appropriate object for the given json. Should never return a GenericFile, but rather a child class.
    """
    @staticmethod
    def load_generic_file(json):
        if json['generic_file_type'] == GenericFile.FILE_TYPE:
            return File(json)
        elif json['generic_file_type'] == GenericFile.DIRECTORY_TYPE:
            return Directory(json)
        elif json['generic_file_type'] == GenericFile.SYMBOLIC_LINK_TYPE:
            return SymbolicLink(json)
        else:
            print('Unsupported file type!')
            return GenericFile(json)

    """
        Establish a connection to mongodb
    """
    def connect(self):
        mongo_path = 'mongodb://' + ','.join(Mongo.configuration.mongo_hosts())
        Mongo.instance = MongoClient(mongo_path)

    """
        Create a generic file in gridfs. No need to return it.
    """
    def create_generic_file(self, generic_file):
        f = self.gridfs.new_file(**generic_file.json)
        f.close()

    """
        Remove a generic file.
    """
    def remove_generic_file(self, generic_file):
        # We cannot directly remove every sub-file in the directory (permissions check to do, ...), but we need to
        # be sure the directory is empty.
        if generic_file.is_dir():
            if self.files_coll.find({'directory':generic_file.filename}).count() != 0:
                raise FuseOSError(errno.ENOTEMPTY)

        # First we delete the file (metadata + chunks)
        self.gridfs.delete(generic_file._id)

        # Then we decrease the number of link in the directory above it
        directory = GenericFile.get_directory(filename=generic_file.filename)
        self.add_nlink_directory(directory=directory, value=-1)

    """
        List files in a given directory. 
    """
    def list_generic_files_in_directory(self, directory):
        files = []
        for elem in self.files_coll.find({'directory':directory}, no_cursor_timeout=True):
            files.append(Mongo.load_generic_file(elem))
        return files

    """
        Indicate if the generic file exists or not. 
    """
    def generic_file_exists(self, filename):
        return self.get_generic_file(filename=filename) is not None

    """
        Retrieve any file / directory / link document from Mongo. Returns None if none are found.
    """
    def get_generic_file(self, filename):
        f = self.files_coll.find_one({'filename': filename})
        if f is not None:
            return Mongo.load_generic_file(f)
        return None
    """
        Increment/reduce the number of links for a directory 
    """
    def add_nlink_directory(self, directory, value):
        # You cannot update directly the object from gridfs, you need to do a MongoDB query instead
        self.files_coll.find_one_and_update({'filename':directory},
                                                         {'$inc':{'metadata.st_nlink':value}})


    """
        Read data from a file 
         file: Instance of a "File" type object.
         offset: Offset from which we want to read the file
         length: Number of bytes we need to send back
        Return bytes array
    """
    def read_data(self, file, offset, size):
        # We get the chunks we are interested in
        chunk_size = file.chunkSize
        starting_chunk = int(floor(offset / chunk_size))
        ending_chunk = int(floor((offset + size) / chunk_size))

        data = b''
        for chunk in self.chunks_coll.find({'files_id':file._id,'n':{'$gte':starting_chunk,'$lte':ending_chunk}}):
            data += chunk['data']
        return data

    """
        Add data to a file. 
         file: Instance of a "File" type object.
         data: bytes 
    """
    def add_data(self, file, data, offset):
        # Normally, we should not update a gridfs document, but re-write everything. I don't see any specific reason
        # to do that, so we will try to update it anyway. But we will only rewrite the last chunks of it, or add information
        # to them, while keeping the limitation of ~255KB/chunk

        # Final size after the update
        total_size = offset + len(data)

        # Important note: the data that we receive are replacing any existing data from "offset".
        chunk_size = file.chunkSize
        total_chunks = int(ceil(file.length / chunk_size))
        starting_chunk = int(floor(offset / chunk_size))
        starting_byte = offset - starting_chunk * chunk_size
        if starting_byte < 0:
            print('Computation error for offset: '+str(offset))
        for chunk in self.chunks_coll.find({'files_id':file._id,'n':{'$gte':starting_chunk}}):
            chunk['data'] = chunk['data'][0:starting_byte] + data[0:chunk_size-starting_byte]
            self.chunks_coll.find_one_and_update({'_id':chunk['_id']},{'$set':{'data':chunk['data']}})

            # We have written a part of what we wanted, we only need to keep the remaining
            data = data[chunk_size-starting_byte:]

            # For the next chunks, we start to replace bytes from zero.
            starting_byte = 0

            # We might not need to go further to write the data
            if len(data) == 0:
                break

        # The code above was only to update a document, we might want to add new chunks
        if len(data) > 0:
            remaining_chunks = int(ceil(len(data) / chunk_size))
            for i in range(0, remaining_chunks):
                chunk = {
                    "files_id": file._id,
                    "data": data[0:chunk_size],
                    "n": total_chunks
                }
                self.chunks_coll.save(chunk)

                # We have written a part of what we wanted, we only the keep the remaining
                data = data[chunk_size:]

                # Next entry
                total_chunks += 1

        # We update the total length and that's it
        self.files_coll.find_one_and_update({'_id':file._id},{'$set':{'length':total_size,'metadata.st_size':total_size}})

        return True

    """
        Truncate a part of a file 
         file: Instance of a "File" type object.
         length: Offset from which we need to truncate the file 
    """
    def truncate(self, file, length):
        # We drop every unnecessary chunk
        chunk_size = file.chunkSize
        maximum_chunks = int(ceil(length / chunk_size))
        self.chunks_coll.delete_many({'files_id':file._id,'n':{'$gte':maximum_chunks}})

        # We update the last chunk
        if length % chunk_size != 0:
            last_chunk = self.chunks_coll.find_one({'files_id':file._id,'n':maximum_chunks-1})
            last_chunk = last_chunk['data'][0:length % chunk_size]
            self.chunks_coll.find_one_and_update({'_id':last_chunk['_id']},{'$set':{'data':last_chunk['data']}})

        # We update the total length and that's it
        self.files_coll.find_one_and_update({'_id':file._id},{'$set':{'length':length,'metadata.st_size':length}})
        return True

    """
        Update some arbitrary fields in the general "files" object
    """
    def basic_save(self, generic_file, metadata):
        self.files_coll.find_one_and_update({'_id':generic_file._id},{'$set':{'metadata':metadata}})

    """
        Clean the database, only for development purposes
    """
    def clean_database(self):
        self.chunks_coll.drop()
        self.files_coll.drop()