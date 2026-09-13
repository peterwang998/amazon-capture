import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from amazon_capture.capture import parse_args
from amazon_capture.config import PROFILE_ENV, default_profile_dir, runtime_paths


class RuntimeConfigurationTests(unittest.TestCase):
    def test_standalone_defaults_do_not_use_unrelated_environment(self):
        root = Path("/tmp/example")
        paths = runtime_paths(root, {"UNRELATED_PROFILE_DIR": "/tmp/other"})
        self.assertEqual(paths.profile, root / "private/browser-profiles/amazon-field-discovery")
        self.assertEqual(paths.output, root / "raw-captures/amazon-field-discovery")

    def test_amazon_profile_environment_and_cli_precedence(self):
        with patch.dict(os.environ, {PROFILE_ENV: "/tmp/example-profile"}, clear=True):
            self.assertEqual(parse_args([]).profile_dir, "/tmp/example-profile")
            self.assertEqual(parse_args(["--profile-dir", "/tmp/explicit"]).profile_dir, "/tmp/explicit")

    def test_defaults_follow_invocation_after_import(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            first = parse_args([])
            with patch("pathlib.Path.cwd", return_value=Path(folder)):
                second = parse_args([])
            self.assertNotEqual(first.output_dir, second.output_dir)
            self.assertEqual(second.output_dir, str(Path(folder) / "raw-captures/amazon-field-discovery"))
            self.assertEqual(second.profile_dir, str(Path(folder) / "private/browser-profiles/amazon-field-discovery"))

    def test_profile_environment_is_evaluated_per_invocation(self):
        with patch.dict(os.environ, {PROFILE_ENV: "/tmp/one"}, clear=True):
            self.assertEqual(parse_args([]).profile_dir, "/tmp/one")
        with patch.dict(os.environ, {PROFILE_ENV: "/tmp/two"}, clear=True):
            self.assertEqual(parse_args([]).profile_dir, "/tmp/two")

    def test_profile_home_directory_expands(self):
        self.assertEqual(default_profile_dir(env={PROFILE_ENV: "~/example-profile"}), Path.home() / "example-profile")
