from collections import defaultdict
from threading import Lock
from time import time
import re


UUID_RE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
HEX_RE = re.compile(r'^[0-9a-fA-F]{16,}$')
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def normalize_path(path: str) -> str:
    parts = []
    for part in (path or '/').strip('/').split('/'):
        if not part:
            continue
        if part.isdigit() or UUID_RE.match(part) or HEX_RE.match(part):
            parts.append('{id}')
        else:
            parts.append(part)
    return '/' + '/'.join(parts) if parts else '/'


def _escape_label(value: str) -> str:
    return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace('"', '\\"')


def _labels(labels: dict[str, str]) -> str:
    if not labels:
        return ''
    return '{' + ','.join(f'{key}="{_escape_label(value)}"' for key, value in sorted(labels.items())) + '}'


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = Lock()
        self._started_at = time()
        self.http_total: dict[tuple[str, str, str], int] = defaultdict(int)
        self.http_duration_count: dict[tuple[str, str], int] = defaultdict(int)
        self.http_duration_sum: dict[tuple[str, str], float] = defaultdict(float)
        self.http_duration_buckets: dict[tuple[str, str, float], int] = defaultdict(int)
        self.events: dict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)

    def observe_http(self, *, method: str, path: str, status_code: int, duration_seconds: float) -> None:
        route = normalize_path(path)
        method = method.upper()
        status = str(status_code)
        with self._lock:
            self.http_total[(method, route, status)] += 1
            self.http_duration_count[(method, route)] += 1
            self.http_duration_sum[(method, route)] += duration_seconds
            for bucket in BUCKETS:
                if duration_seconds <= bucket:
                    self.http_duration_buckets[(method, route, bucket)] += 1

    def increment(self, name: str, labels: dict[str, str] | None = None, value: int = 1) -> None:
        label_items = tuple(sorted((labels or {}).items()))
        with self._lock:
            self.events[(name, label_items)] += value

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            uptime = time() - self._started_at
            lines.extend([
                '# HELP processing_platform_uptime_seconds Process uptime in seconds.',
                '# TYPE processing_platform_uptime_seconds gauge',
                f'processing_platform_uptime_seconds {uptime:.3f}',
                '# HELP processing_platform_http_requests_total Total HTTP requests.',
                '# TYPE processing_platform_http_requests_total counter',
            ])
            for (method, path, status), value in sorted(self.http_total.items()):
                lines.append(f'processing_platform_http_requests_total{_labels({"method": method, "path": path, "status": status})} {value}')

            lines.extend([
                '# HELP processing_platform_http_request_duration_seconds HTTP request duration histogram.',
                '# TYPE processing_platform_http_request_duration_seconds histogram',
            ])
            for (method, path), count in sorted(self.http_duration_count.items()):
                cumulative = 0
                for bucket in BUCKETS:
                    cumulative = self.http_duration_buckets.get((method, path, bucket), cumulative)
                    labels = {'method': method, 'path': path, 'le': str(bucket)}
                    lines.append(f'processing_platform_http_request_duration_seconds_bucket{_labels(labels)} {cumulative}')
                labels = {'method': method, 'path': path, 'le': '+Inf'}
                lines.append(f'processing_platform_http_request_duration_seconds_bucket{_labels(labels)} {count}')
                lines.append(f'processing_platform_http_request_duration_seconds_count{_labels({"method": method, "path": path})} {count}')
                lines.append(f'processing_platform_http_request_duration_seconds_sum{_labels({"method": method, "path": path})} {self.http_duration_sum[(method, path)]:.6f}')

            for (name, label_items), value in sorted(self.events.items()):
                labels = dict(label_items)
                lines.append(f'# TYPE {name} counter')
                lines.append(f'{name}{_labels(labels)} {value}')
        return '\n'.join(lines) + '\n'


metrics_registry = MetricsRegistry()
