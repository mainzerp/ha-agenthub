"""Unit tests for main.py helper functions.

Tests _ensure_log_buffer_handler and _configure_logging.
"""

from __future__ import annotations

import logging
import os
from unittest.mock import patch

from app.main import _configure_logging, _ensure_log_buffer_handler
from app.util.log_buffer import LogBufferHandler


class TestEnsureLogBufferHandler:
    def test_ensure_log_buffer_handler_adds_handler_when_missing(self):
        root = logging.getLogger()
        # Remove any existing LogBufferHandler
        for h in list(root.handlers):
            if isinstance(h, LogBufferHandler):
                root.removeHandler(h)

        _ensure_log_buffer_handler()

        assert any(isinstance(h, LogBufferHandler) for h in root.handlers)

    def test_ensure_log_buffer_handler_does_not_duplicate(self):
        root = logging.getLogger()
        # Ensure exactly one exists after first call
        _ensure_log_buffer_handler()
        count_before = sum(1 for h in root.handlers if isinstance(h, LogBufferHandler))

        _ensure_log_buffer_handler()

        count_after = sum(1 for h in root.handlers if isinstance(h, LogBufferHandler))
        assert count_before == count_after == 1


class TestConfigureLogging:
    def test_configure_logging_minimal_config(self, tmp_path):
        root = logging.getLogger()
        # Clear handlers to test minimal config path
        original_handlers = list(root.handlers)
        for h in original_handlers:
            root.removeHandler(h)

        with patch.dict(os.environ, {"LOG_DIR": str(tmp_path)}):
            _configure_logging()

        # Should have at least StreamHandler and RotatingFileHandler and LogBufferHandler
        assert any(isinstance(h, logging.StreamHandler) and not isinstance(h, LogBufferHandler) for h in root.handlers)
        assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers)
        assert any(isinstance(h, LogBufferHandler) for h in root.handlers)

        # Restore original handlers
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in original_handlers:
            root.addHandler(h)

    def test_configure_logging_sets_root_level(self, tmp_path):
        root = logging.getLogger()
        original_level = root.level
        original_handlers = list(root.handlers)
        for h in original_handlers:
            root.removeHandler(h)

        with patch.dict(os.environ, {"LOG_DIR": str(tmp_path)}), patch("app.main.settings.log_level", "DEBUG"):
            _configure_logging()

        assert root.level == logging.DEBUG

        # Restore
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in original_handlers:
            root.addHandler(h)
        root.setLevel(original_level)
