import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from atomic_files import replace_file


class AtomicFileTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.source=Path(self.temp.name)/"next.tmp"
        self.target=Path(self.temp.name)/"products.json"
        self.source.write_text("new")
        self.target.write_text("old")
    def tearDown(self):
        self.temp.cleanup()

    def test_transient_windows_lock_is_retried(self):
        original=os.replace
        error=PermissionError("temporarily locked")
        error.winerror=32
        calls=[]
        def replace(source,target):
            calls.append(1)
            if len(calls)<3:
                raise error
            original(source,target)
        with patch("atomic_files.os.replace",side_effect=replace),patch("atomic_files.time.sleep"):
            replace_file(self.source,self.target)
        self.assertEqual(self.target.read_text(),"new")
        self.assertEqual(len(calls),3)

    def test_permanent_denial_preserves_previous_file(self):
        error=PermissionError("denied")
        error.winerror=5
        with patch("atomic_files.os.replace",side_effect=error) as replace,patch("atomic_files.time.sleep"):
            with self.assertRaises(PermissionError):
                replace_file(self.source,self.target)
            self.assertEqual(replace.call_count,5)
        self.assertEqual(self.target.read_text(),"old")
        self.assertEqual(self.source.read_text(),"new")

    def test_unrelated_failure_is_not_retried(self):
        with patch("atomic_files.os.replace",side_effect=OSError("disk full")) as replace:
            with self.assertRaises(OSError):
                replace_file(self.source,self.target)
            self.assertEqual(replace.call_count,1)


if __name__=="__main__":
    unittest.main()
