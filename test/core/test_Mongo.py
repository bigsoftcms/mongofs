import unittest
import json
from bson import json_util
from fuse import FuseOSError

from src.core.Configuration import Configuration
from src.core.Mongo import Mongo
from src.core.GenericFile import GenericFile
from src.core.File import File
from src.core.Directory import Directory
from src.core.SymbolicLink import SymbolicLink

class TestMongo(unittest.TestCase):
    def setUp(self):
        Configuration.FILEPATH = 'test/resources/conf/mongofs.json'
        self.obj = Mongo()
        self.load_files()

    def load_files(self):
        # Load various files as setUp
        with open('test/resources/data/file.json', 'r') as f:
            self.file_raw = json_util.loads(f.read())
        self.file = self.obj.load_generic_file(self.file_raw)

        with open('test/resources/data/file-chunks.json', 'r') as f:
            self.file_chunks_raw = json_util.loads(f.read())

        with open('test/resources/data/directory.json', 'r') as f:
            self.directory_raw = json_util.loads(f.read())
        self.directory = self.obj.load_generic_file(self.directory_raw)

        with open('test/resources/data/symbolic-link.json', 'r') as f:
            self.symbolic_link_raw = json_util.loads(f.read())
        self.symbolic_link_file = self.obj.load_generic_file(self.symbolic_link_raw)

        with open('test/resources/data/directory-file.json', 'r') as f:
            self.directory_file_raw = json_util.loads(f.read())
        self.directory_file = self.obj.load_generic_file(self.directory_file_raw)

    def insert_file(self):
        self.obj.files_coll.insert_one(self.file_raw)

    def insert_file_chunks(self):
        self.obj.chunks_coll.insert_many(self.file_chunks_raw)

    def insert_directory(self):
        self.obj.files_coll.insert_one(self.directory_raw)

    def insert_directory_file(self):
        self.obj.files_coll.insert_one(self.directory_file_raw)

    def insert_symbolic_link(self):
        self.obj.files_coll.insert_one(self.symbolic_link_raw)

    def read_file_chunks(self):
        message = b''
        for chunk in self.obj.chunks_coll.find({'files_id':self.file._id}):
            message += chunk['data']
        return message

    def tearDown(self):
        self.obj.clean_database()

    def test_connect(self):
        # Normally the "setUp" should have already created a connection
        self.obj.connect()
        self.assertEqual(list(self.obj.files_coll.find({'_id':'0'})), [])

    def test_load_generic_file_file(self):
        self.assertIsInstance(self.obj.load_generic_file(self.file_raw), File)

    def test_load_generic_file_directory(self):
        self.assertIsInstance(self.obj.load_generic_file(self.directory_raw), Directory)

    def test_load_generic_file_symbolic_link(self):
        self.assertIsInstance(self.obj.load_generic_file(self.symbolic_link_raw), SymbolicLink)

    def test_current_user(self):
        user = self.obj.current_user()
        self.assertIsInstance(user['uid'], int)
        self.assertIsInstance(user['gid'], int)
        self.assertIsInstance(user['pid'], int)

    def test_lock_id(self):
        filename = 'test-file'
        hostname = Mongo.configuration.hostname()
        pid = self.obj.current_user()['pid']
        expected_lock = filename+';'+str(pid)+';'+hostname
        self.assertEqual(self.obj.lock_id(filename=filename), expected_lock)

    def test_master_lock_id(self):
        filename = 'test-file'
        hostname = Mongo.configuration.hostname()
        pid = 0
        expected_lock = filename + ';' + str(pid) + ';' + hostname
        self.assertEqual(self.obj.master_lock_id(filename=filename), expected_lock)

    def test_create_generic_file(self):
        self.insert_file()
        gf = self.obj.files_coll.find_one({'filename':self.file.filename},{'uploadDate':False})
        self.assertEqual(json_util.dumps(gf), json_util.dumps(self.file_raw))

    def test_remove_generic_file(self):
        self.insert_file()
        self.obj.remove_generic_file(generic_file=self.file)
        gf = self.obj.files_coll.find_one({'filename': self.file.filename})
        self.assertEqual(gf, None)

    def test_remove_generic_file_directory_not_empty(self):
        # Try to delete the parent directory while a file still exist in it
        self.insert_directory()
        self.obj.create_generic_file(generic_file=self.directory_file)
        try:
            self.obj.remove_generic_file(generic_file=self.directory)
            self.assertTrue(False, msg="It was possible to remove a directory while it was still containing files.")
        except FuseOSError as e:
            self.assertTrue(True)

    def test_remove_generic_file_directory_empty(self):
        # Try to delete the parent directory after deleting the file in it
        self.insert_directory()
        self.insert_directory_file()
        self.obj.remove_generic_file(generic_file=self.directory_file)
        self.obj.remove_generic_file(generic_file=self.directory)
        self.assertTrue(True)

    def test_list_generic_files_in_directory(self):
        self.insert_directory()
        self.insert_file()
        self.insert_symbolic_link()

        files = self.obj.list_generic_files_in_directory(directory='/')
        self.assertEqual(len(files), 3)

    def test_generic_file_exists(self):
        self.assertFalse(self.obj.generic_file_exists(self.file.filename))
        self.insert_file()
        self.assertTrue(self.obj.generic_file_exists(self.file.filename))

    def test_get_generic_file(self):
        self.insert_file()
        gf = self.obj.get_generic_file(filename=self.file.filename)
        self.assertIsInstance(gf, File)

    def test_get_generic_file_take_lock(self):
        self.insert_file()
        gf = self.obj.get_generic_file(filename=self.file.filename, take_lock=True)
        self.assertIsInstance(gf, File)

        # We are the same owner, so normally, we should still be able to take the file if there is a lock on it.
        gf = self.obj.get_generic_file(filename=self.file.filename, take_lock=True)
        self.assertIsInstance(gf, File)

    def test_get_generic_file_missing(self):
        gf = self.obj.get_generic_file(filename=self.file.filename)
        self.assertEqual(gf, None)

    def test_add_nlink_directory(self):
        # By default, a directory has 2 st_nlink. And by default, the "/" directory always exists.
        self.obj.add_nlink_directory(directory='/', value=4)
        gf = self.obj.files_coll.find_one({'filename':'/'})
        self.assertEqual(gf['metadata']['st_nlink'], 6)

    def test_read_data(self):
        self.insert_file()
        self.insert_file_chunks()
        message = self.read_file_chunks()

        data = self.obj.read_data(file=self.file, offset=0, size=4096)
        self.assertEqual(data, message)

        data = self.obj.read_data(file=self.file, offset=3, size=4096)
        self.assertEqual(data, message[3:])

        data = self.obj.read_data(file=self.file, offset=0, size=8)
        self.assertEqual(data, message[:8])

        data = self.obj.read_data(file=self.file, offset=3, size=8)
        self.assertEqual(data, message[3:8])

    def test_add_data_append(self):
        self.insert_file()
        self.insert_file_chunks()
        message = self.read_file_chunks()

        self.obj.add_data(file=self.file, data=b'test', offset=len(message))
        modified_message = self.read_file_chunks()
        self.assertEqual(modified_message, message+b'test')

    def test_add_data_replace(self):
        self.insert_file()
        self.insert_file_chunks()
        message = self.read_file_chunks()
        message = list(message)
        message[2] = ord('t')
        message[3] = ord('h')
        message[4] = ord('i')
        message[5] = ord('n')
        message[6] = ord('g')
        message[7] = ord('s')
        expected_message = ''.join(map(chr, message))

        self.obj.add_data(file=self.file, data=b'things', offset=2)
        modified_message = self.read_file_chunks()
        formatted_modified_message = ''.join(map(chr, list(modified_message)))
        self.assertEqual(formatted_modified_message, expected_message)

    def test_truncate(self):
        self.insert_file()
        self.insert_file_chunks()
        message = self.read_file_chunks()

        self.obj.truncate(file=self.file, length=6)
        modified_message = self.read_file_chunks()
        self.assertEqual(modified_message, message[0:6])

    def test_truncate_zero(self):
        self.insert_file()
        self.insert_file_chunks()
        message = self.read_file_chunks()

        self.obj.truncate(file=self.file, length=0)
        modified_message = self.read_file_chunks()
        self.assertEqual(modified_message, message[0:0])

    def test_rename_generic_file_to(self):
        self.insert_file()
        self.insert_file_chunks()
        message = self.read_file_chunks()

        self.obj.rename_generic_file_to(generic_file=self.file, destination_filename='rename-test')
        destination_message = self.read_file_chunks() # Read chunks is based on the _id, so we can call it
        # Normally, the chunks should not change at all, but we never know.
        self.assertEqual(destination_message, message)

        old_file = self.obj.files_coll.find_one({'filename':self.file.filename})
        self.assertEqual(old_file, None)

        new_file = self.obj.files_coll.find_one({'filename': 'rename-test'})
        self.assertNotEqual(new_file, None)

    def test_unlock_generic_file(self):
        self.insert_file()
        gf = self.obj.get_generic_file(filename=self.file.filename, take_lock=True)
        result = self.obj.unlock_generic_file(generic_file=gf)
        self.assertTrue(result)

        gf = self.obj.get_generic_file(filename=self.file.filename)
        self.assertTrue('lock' not in gf.json)

    def test_unlock_generic_file_no_lock(self):
        # Verify if it does not crash if there is no lock
        self.insert_file()
        gf = self.obj.get_generic_file(filename=self.file.filename, take_lock=False)
        result = self.obj.unlock_generic_file(generic_file=gf)
        self.assertTrue(result)

    def test_basic_save(self):
        self.insert_file()
        self.obj.basic_save(generic_file=self.file, metadata={'st_nlink':1}, attrs={'thing':1})
        result = self.obj.files_coll.find_one({'filename':self.file.filename})
        self.assertTrue('st_nlink' in result['metadata'] and len(result['metadata']) == 1)
        self.assertTrue('thing' in result['attrs'] and len(result['attrs']) == 1)


if __name__ == '__main__':
    unittest.main()