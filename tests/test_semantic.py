"""One-command semantic search: the daemon's loop, the CLI and the honest off states."""

import argparse
import contextlib
import importlib.metadata
import io
import unittest
from unittest.mock import patch

from test_review_cli import DaemonCase

from memoryking import __main__ as cli
from memoryking import semantic
from memoryking.domain.local import LocalError


class FakeModel:
    """Stands in for the loaded model: the fingerprint of the real manifest, vectors by hand."""

    kinds = True
    name = semantic.MODEL

    def __init__(self):
        self.fingerprint = semantic.fingerprint_of()
        self.dimensions = 384

    def embed(self, texts, kind="document"):
        return type("Batch", (), {"vectors": [[1000000] + [0] * 383 for _ in texts]})()


class SemanticTests(DaemonCase):
    def partitions(self):
        with self.store.transaction():
            return {
                row["project"]: row["state"]
                for row in self.store.db.execute(
                    "SELECT project,state FROM embedding_partitions"
                ).fetchall()
            }

    def test_status_says_off_and_how_to_turn_it_on_when_no_model_is_loaded(self):
        text = self.cli("status")
        self.assertIn("Semantic   off", text)
        self.assertIn("memoryking semantic enable", text)

    def test_the_loop_enables_each_project_with_memory_but_never_one_an_owner_disabled(self):
        from memoryking.embedding_loop import EmbeddingLoop

        self.capture("a", "The release train leaves every Thursday.")
        self.store.embedder = FakeModel()
        loop = EmbeddingLoop(self.store, self.server.path, self.owner_file, self.store.embedder)
        loop.enable_new_projects()
        self.assertEqual(self.partitions(), {"p": "experimental"})
        # An owner turns the project off; the loop must not turn it back on.
        args = argparse.Namespace(socket=self.server.path)
        token = self.owner
        cli.semantic_configure(args, token, "p", "disabled")
        self.assertEqual(self.partitions(), {"p": "disabled"})
        loop.enabled_at = 0.0
        loop.enable_new_projects()
        self.assertEqual(self.partitions(), {"p": "disabled"})

    def test_status_reports_progress_once_a_model_is_loaded_and_a_project_enabled(self):
        self.capture("a", "The release train leaves every Thursday.")
        self.store.embedder = FakeModel()
        args = argparse.Namespace(socket=self.server.path)
        cli.semantic_configure(args, self.owner, "p", "experimental")
        text = self.cli("status")
        self.assertIn("Semantic   " + semantic.MODEL, text)
        self.assertIn("p 0 vectors, 0% indexed", text)

    def test_enable_installs_nothing_when_the_runtime_is_missing_and_says_so(self):
        output = io.StringIO()
        args = argparse.Namespace(
            semantic_command="enable",
            socket=self.server.path,
            credential=self.owner_file,
            project=None,
            profile=None,
            config=None,
        )
        with (
            patch.object(
                importlib.metadata,
                "version",
                side_effect=importlib.metadata.PackageNotFoundError("onnxruntime"),
            ),
            patch("memoryking.model_install.install") as install,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(cli.semantic_command(args), 1)
        install.assert_not_called()
        self.assertIn("optional embeddings extra is not installed", output.getvalue())
        self.assertIn("Search stays lexical", output.getvalue())

    def test_enable_states_what_it_downloads_then_loads_and_switches_the_project_on(self):
        output = io.StringIO()
        args = argparse.Namespace(
            semantic_command="enable",
            socket=self.server.path,
            credential=self.owner_file,
            project="p",
            profile=None,
            config=None,
        )
        self.store.embedder = FakeModel()  # the daemon already holds the model
        installed = {"status": "installed_verified"}
        with (
            patch.object(semantic, "runtime_problem", return_value=None),
            patch.object(semantic, "verified", return_value=False),
            patch("memoryking.model_install.install", return_value=installed) as install,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(cli.semantic_command(args), 0)
        install.assert_called_once()
        text = output.getvalue()
        # The consent is in the output, before the work: size, licence, host, revision, hashes.
        self.assertIn("Downloading " + semantic.MODEL, text)
        self.assertIn("huggingface.co", text)
        self.assertIn("pinned SHA-256", text)
        self.assertIn("never downloads a model on its own", text)
        self.assertIn("Semantic search is on for project p", text)
        self.assertEqual(self.partitions(), {"p": "experimental"})

    def test_an_installed_model_is_loaded_without_a_flag_and_a_bad_one_is_not(self):
        with patch.object(semantic, "runtime_problem", return_value=None):
            with patch.object(semantic, "verified", return_value=False):
                self.assertIsNone(semantic.load_installed())
        with patch.object(semantic, "runtime_problem", return_value="no runtime"):
            self.assertIsNone(semantic.load_installed())

    def test_setup_downloads_nothing_without_the_runtime_in_a_dry_run_or_when_declined(self):
        from memoryking import setup_host

        def step(**changes):
            say = {}
            options = {"no_semantic": False, "dry_run": False, "project": "p", **changes}
            with patch("memoryking.model_install.install") as install:
                setup_host.semantic_step("x", argparse.Namespace(**options), {}, say)
            install.assert_not_called()
            return say["semantic"]

        with patch.object(semantic, "runtime_problem", return_value="the extra is not installed"):
            text = step()
        self.assertIn("search stays lexical", text)
        self.assertIn("memoryking semantic enable", text)
        with patch.object(semantic, "runtime_problem", return_value=None):
            self.assertIn("would install", step(dry_run=True))
            self.assertIn("pinned SHA-256", step(dry_run=True))
            self.assertIn("skipped", step(no_semantic=True))

    def test_an_unsupported_model_name_is_refused(self):
        with self.assertRaises(LocalError):
            semantic.model_dir("not-a-model")

    def test_consent_names_the_licence_size_and_host(self):
        text = semantic.consent()
        self.assertIn(semantic.model_manifest(semantic.MODEL)["license"], text)
        self.assertIn("MB", text)
        self.assertIn("huggingface.co", text)


if __name__ == "__main__":
    unittest.main()
