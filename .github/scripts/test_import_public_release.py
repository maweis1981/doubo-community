import hashlib
import importlib.util
import io
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

spec = importlib.util.spec_from_file_location("import_release", Path(__file__).with_name("import_public_release.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ReleaseValidationTests(unittest.TestCase):
    def fixture(self):
        tag = "android-v0.1.0-2"
        name = "Doubo-Android-0.1.0-2.apk"
        digest = hashlib.sha256(b"package").hexdigest()
        assets = [dict(name=n, size=7, digest="sha256:" + digest,
                       browser_download_url=f"https://github.com/example/public/releases/download/{tag}/{n}")
                  for n in (name, name + ".sha256")]
        return tag, digest, dict(tag_name=tag, draft=False, published_at="2026-01-01", prerelease=False, assets=assets)

    def test_public_pair(self):
        tag, digest, release = self.fixture()
        self.assertEqual(module.validate_source("example/public", tag, digest,
                         {"private": False, "visibility": "public", "full_name": "example/public"}, release)[0]["name"],
                         "Doubo-Android-0.1.0-2.apk")

    def test_reject_private_draft_unpaired_or_foreign_asset(self):
        for mutation in ("private", "draft", "extra", "foreign", "digest"):
            tag, digest, release = self.fixture()
            repo = {"private": False, "visibility": "public", "full_name": "example/public"}
            if mutation == "private": repo["private"] = True
            if mutation == "draft": release["draft"] = True
            if mutation == "extra": release["assets"].append(release["assets"][0].copy())
            if mutation == "foreign": release["assets"][0]["browser_download_url"] = "https://example.com/package"
            if mutation == "digest": release["assets"][0]["digest"] = "sha256:" + "0" * 64
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                module.validate_source("example/public", tag, digest, repo, release)

    def test_checksum_rejects_wrong_filename_hash_and_extra_lines(self):
        digest = hashlib.sha256(b"package").hexdigest()
        module.validate_checksum(f"{digest}  package.apk\n", "package.apk", digest)
        for value in (f"{digest}  ../package.apk", f"{'0' * 64}  package.apk", f"{digest}  package.apk\nextra"):
            with self.assertRaises(ValueError): module.validate_checksum(value, "package.apk", digest)

    def test_existing_assets_never_overwritten(self):
        wanted = [{"name": "package.apk", "size": 7, "digest": "sha256:" + "a" * 64}]
        self.assertEqual(module.missing_assets(wanted, wanted, False), [])
        with self.assertRaises(ValueError): module.missing_assets(wanted, [], False)
        with self.assertRaises(ValueError):
            module.missing_assets(wanted, [dict(wanted[0], digest="sha256:" + "b" * 64)], True)
        self.assertEqual(module.missing_assets(wanted, [], True), wanted)

    def test_download_verifies_actual_bytes(self):
        payload = b"verified package"
        asset = {"name": "package.apk", "size": len(payload),
                 "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                 "browser_download_url": "https://github.com/example/public/releases/download/android-v1.0.0-1/package.apk"}
        for content, valid in ((payload, True), (b"tampered package", False), (payload[:-1], False)):
            with tempfile.TemporaryDirectory() as folder, patch.object(module.urllib.request, "urlopen", return_value=io.BytesIO(content)) as request:
                if valid:
                    self.assertEqual(module.download(asset, Path(folder)).read_bytes(), payload)
                else:
                    with self.assertRaises(ValueError): module.download(asset, Path(folder))
                request.assert_called_once_with(asset["browser_download_url"], timeout=120)


if __name__ == "__main__":
    unittest.main()
