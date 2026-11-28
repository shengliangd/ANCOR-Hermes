"""Pluggable multi-channel notification sinks and fan-out dispatcher."""
from .base import NotificationPayload, NotificationSink, SinkError, SinkUnavailable
from .command import CommandNotifier
from .dispatcher import MultiNotificationDispatcher, build_notifiers
from .webhook import WebhookNotifier

__all__ = [
    'CommandNotifier',
    'MultiNotificationDispatcher',
    'NotificationPayload',
    'NotificationSink',
    'SinkError',
    'SinkUnavailable',
    'WebhookNotifier',
    'build_notifiers',
]
