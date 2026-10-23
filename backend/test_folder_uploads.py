"""Offline folder upload checks, including traversal and decompression limits."""
import io
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import folder_uploads
from app.script_catalog import discover_scripts, script_command


def archive(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path, content in files:
            bundle.writestr(path, content)
    return stream.getvalue()


class FolderUploadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="folder-upload-test-")
        self.root = Path(temporary.name)
        self.addCleanup(temporary.cleanup)
        self.path_patch = patch.object(folder_uploads, "DATA_DIR", self.root)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        app = FastAPI()
        app.include_router(folder_uploads.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def upload(self, files):
        return self.client.post("/api/files/upload-folder", content=archive(files), headers={"Content-Type": "application/zip"})

    def assert_clean(self):
        self.assertEqual(list((self.root / "script_uploads").iterdir()), [])

    def test_hierarchy_unicode_content_and_deduplication(self):
        files = [("模型脚本/main.py", "print('offline')"), ("模型脚本/fixtures/data.json", '{"ok":true}'), ("模型脚本/empty.txt", "")]
        first = self.upload(files)
        self.assertEqual(first.status_code, 200, first.text)
        result = first.json()
        self.assertEqual(result["file_count"], 3)
        path = Path(result["path"])
        self.assertEqual(path.name, "模型脚本")
        self.assertEqual((path / "fixtures/data.json").read_text(), '{"ok":true}')
        self.assertTrue((path / "main.py").stat().st_mode & stat.S_IXUSR)
        second = self.upload(list(reversed(files))).json()
        self.assertEqual(second["path"], result["path"])
        self.assertTrue(second["reused"])
        self.assertEqual(len(list((self.root / "script_uploads").iterdir())), 1)

    def test_changed_scripts_are_not_overwritten_or_reused(self):
        files = [("scripts/main.py", "print('original')")]
        result = self.upload(files).json()
        path = Path(result["path"]) / "main.py"
        path.write_text("changed during execution")
        second = self.upload(files).json()
        self.assertNotEqual(second["path"], result["path"])
        self.assertEqual(path.read_text(), "changed during execution")
        self.assertEqual((Path(second["path"]) / "main.py").read_text(), "print('original')")

    def test_recognizes_nested_scripts_and_preserves_supporting_files(self):
        result = self.upload([('root/a/test.PY', 'print("ok")'), ('root/b/test.py', 'print("ok")'),
                              ('root/a/run.bash', 'items=(ok); echo "${items[0]}"'),
                              ('root/a/helper', '#!/usr/bin/env python3\nprint("ok")'),
                              ('root/a/__init__.py', ''), ('root/report.html', '<html>report</html>'),
                              ('root/data.csv', 'a,b'), ('root/node_modules/skip.py', 'raise Exception()'),
                              ('root/__pycache__/skip.py', 'raise Exception()')]).json()
        self.assertEqual(result['script_count'], 5)
        self.assertEqual(result['runnable_count'], 4)
        self.assertEqual(result['other_count'], 2)
        scripts = {item['name']: item for item in result['scripts']}
        self.assertIn('b/test.py', scripts)
        self.assertFalse(scripts['a/__init__.py']['runnable'])
        working_dir, command = script_command(Path(result['path']), scripts['a/run.bash'])
        self.assertEqual(Path(working_dir).name, 'a')
        self.assertEqual(Path(command[0]).name, 'bash')
        self.assertTrue((Path(result['path']) / 'data.csv').exists())

    def test_flat_zip_upload_recognizes_files_in_multiple_roots(self):
        response = self.client.post('/api/files/upload-folder?archive=true', content=archive([
            ('test.py', 'print("ok")'), ('nested/test.sh', 'echo ok'), ('data.json', '{}')]))
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['script_count'], 2)
        self.assertEqual(result['other_count'], 1)
        self.assertEqual({item['name'] for item in result['scripts']}, {'test.py', 'nested/test.sh'})
        self.assertTrue((Path(result['path']) / 'data.json').exists())
        self.assertEqual(self.client.post('/api/files/upload-folder?archive=true', content=archive([('../escape.py', '')])).status_code, 400)

    def test_missing_interpreter_is_reported_and_symlinks_are_not_discovered(self):
        folder = self.root / 'fixture'
        folder.mkdir()
        (folder / 'check.js').write_text('console.log("ok")')
        (folder / 'data.txt').write_text('data')
        (folder / 'link.py').symlink_to(folder / 'data.txt')
        with patch('app.script_catalog.runtime', return_value=None):
            result = discover_scripts(folder)
        self.assertEqual(result['script_count'], 1)
        self.assertEqual(result['runnable_count'], 0)
        self.assertEqual(result['scripts'][0]['reason'], '服务器未安装 node')

    def test_invalid_paths_duplicates_and_multiple_roots_leave_no_files(self):
        for files in ([('/tmp/escape.py', 'x')], [('root/../escape.py', 'x')], [('root\\escape.py', 'x')],
                      [('C:/escape.py', 'x')], [('root/a.py', 'x'), ('other/b.py', 'y')],
                      [('root/a.py', 'x'), ('root/a.py', 'y')], [('a.py', 'x')], []):
            with self.subTest(files=files):
                self.assertEqual(self.upload(files).status_code, 400)
                self.assert_clean()

    def test_symbolic_links_are_rejected(self):
        link = zipfile.ZipInfo('scripts/link')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.assertEqual(self.upload([(link, '/etc/passwd')]).status_code, 400)
        self.assert_clean()

    def test_size_file_count_and_stream_limits_leave_no_files(self):
        cases = [("MAX_TOTAL", 8, [('root/a.txt', '0' * 5), ('root/b.txt', '0' * 5)]),
                 ("MAX_FILE", 8, [('root/a.txt', '0' * 9)]),
                 ("MAX_FILES", 1, [('root/a.txt', 'a'), ('root/b.txt', 'b')]),
                 ("MAX_ARCHIVE", 8, [('root/a.txt', 'a')])]
        for setting, limit, files in cases:
            with self.subTest(setting=setting), patch.object(folder_uploads, setting, limit):
                self.assertEqual(self.upload(files).status_code, 413)
                self.assert_clean()
        self.assertEqual(self.client.post('/api/files/upload-folder', content=b'not a zip').status_code, 400)
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
