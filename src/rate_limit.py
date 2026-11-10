"""Shared rate limiting for outbound API calls and subprocess tools.

One process-wide `APIRateLimiter` tracks per-service backoff so that a 429
from, say, MusicBrainz pauses every caller of MusicBrainz in this process,
not just the one that received it.
"""
import functools
import logging
import subprocess
import time
from datetime import datetime
from typing import List, Optional

import requests

logger = logging.getLogger('musicai.app')


class APIRateLimiter:
    """
    Rate limiter to prevent hitting API rate limits when multiple users make requests.
    Implements exponential backoff and per-user throttling.
    """
    def __init__(self):
        self.min_delay = 2.0  # Minimum delay between API calls (seconds)
        self.max_delay = 30.0  # Maximum delay between API calls
        self.last_request_time = {}  # Per-user tracking
        self.rate_limit_hit = {}  # Track when rate limits are hit
        self.request_count = {}  # Track request count per service
        self.rate_limit_info = {}  # Track rate limit headers (limit, used, remaining)
    
    def get_delay(self, service_name='default'):
        """Get the delay to apply before next API request"""
        now = time.time()
        last_request = self.last_request_time.get(service_name, 0)
        time_since_last = now - last_request
        
        # Check if we hit a rate limit recently
        if service_name in self.rate_limit_hit:
            hit_time = self.rate_limit_hit[service_name]
            retry_after = hit_time.get('retry_after', 5)
            if now - hit_time['time'] < retry_after:
                # Still in rate limit backoff period
                return max(retry_after - (now - hit_time['time']), 1.0)
        
        # Normal throttling
        if time_since_last < self.min_delay:
            return self.min_delay - time_since_last
        
        return 0
    
    def wait_if_needed(self, service_name='default'):
        """Wait before making next API request if needed"""
        delay = self.get_delay(service_name)
        if delay > 0.1:
            # If wait is long, provide periodic countdown logs so users know what's happening
            if delay > 5:
                logger.info(f"⏳ Rate limiting {service_name}: waiting {int(delay)}s before next request")
                remaining = int(delay)
                while remaining > 0:
                    # Log every 5s and the final 5s countdown
                    if remaining % 5 == 0 or remaining <= 5:
                        logger.info(f"  {remaining}s remaining before {service_name} requests resume")
                    time.sleep(1)
                    remaining -= 1
            else:
                logger.debug(f"Rate limiting {service_name}: waiting {delay:.1f}s")
                time.sleep(delay)
        
        self.last_request_time[service_name] = time.time()

    def format_time(self, seconds):
        """Format seconds to human-readable format"""
        if seconds < 60:
            return f"{seconds:.0f}s"
        elif seconds < 3600:
            minutes = seconds / 60
            return f"{minutes:.1f}m"
        else:
            hours = seconds / 3600
            return f"{hours:.1f}h"

    def track_request(self, service_name='default'):
        """Track API request count per service"""
        if service_name not in self.request_count:
            self.request_count[service_name] = 0
        self.request_count[service_name] += 1


    def on_rate_limit(self, service_name='default', retry_after=5):
        """Call when API returns rate limit error"""
        reset_time = time.time() + retry_after
        self.rate_limit_hit[service_name] = {
            'time': time.time(),
            'retry_after': retry_after
        }
        formatted_time = self.format_time(retry_after)
        reset_datetime = datetime.fromtimestamp(reset_time).strftime('%H:%M:%S')
        
        # Build detailed message with rate limit info
        message = f"⚠️  Rate limit hit on {service_name}: backoff {formatted_time} (resets at {reset_datetime})"
        
        # Add request count information
        if service_name in self.request_count:
            message += f" | Total requests made: {self.request_count[service_name]}"
        
        # Add rate limit info if available
        if service_name in self.rate_limit_info:
            info = self.rate_limit_info[service_name]
            if 'limit' in info or 'used' in info or 'remaining' in info:
                limits_str = []
                if 'limit' in info:
                    limits_str.append(f"Limit: {info['limit']}")
                if 'used' in info:
                    limits_str.append(f"Used: {info['used']}")
                if 'remaining' in info:
                    limits_str.append(f"Remaining: {info['remaining']}")
                if limits_str:
                    message += f" | {' | '.join(limits_str)}"
        
        logger.warning(message)


def is_rate_limit_error(e) -> bool:
    """Detects common rate limit error signatures across libraries"""
    # requests
    if isinstance(e, requests.exceptions.HTTPError):
        resp = getattr(e, 'response', None)
        if resp is not None:
            return getattr(resp, 'status_code', None) == 429

    # subprocess or generic message checks
    msg = str(e).lower()
    if 'rate limit' in msg or 'too many requests' in msg or '429' in msg:
        return True

    return False


def handle_rate_limit(service_name: str, e: Exception, attempt: int, max_retries: int):
    """Central handling when a rate limit is detected"""
    retry_after = 60
    # Try to extract Retry-After header if present
    try:
        headers = None
        if hasattr(e, 'headers'):
            headers = e.headers
        elif hasattr(e, 'response') and getattr(e.response, 'headers', None):
            headers = e.response.headers
        if headers and 'Retry-After' in headers:
            retry_after = int(headers.get('Retry-After', retry_after))
    except Exception:
        pass

    rate_limiter = get_rate_limiter()
    rate_limiter.on_rate_limit(service_name, retry_after=retry_after)

    # Countdown logging handled by APIRateLimiter.wait_if_needed, but here we provide context
    logger.info(f"⚠️  Backing off {service_name} for {retry_after}s (attempt {attempt+1}/{max_retries})")
    # Sleep with small increments to be responsive to interrupts
    remaining = int(retry_after)
    while remaining > 0:
        if remaining % 5 == 0 or remaining <= 5:
            logger.info(f"  {remaining}s remaining before retrying {service_name}")
        time.sleep(1)
        remaining -= 1


def with_rate_limit(service_name: str = 'default', max_retries: int = 3):
    """Decorator to add rate limiting and retry to any API call"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            rate_limiter = get_rate_limiter()
            for attempt in range(max_retries):
                try:
                    rate_limiter.wait_if_needed(service_name)
                    return func(*args, **kwargs)
                except Exception as e:
                    if is_rate_limit_error(e):
                        handle_rate_limit(service_name, e, attempt, max_retries)
                        continue
                    raise
            logger.warning(f"✖ {func.__name__}: exceeded max retries ({max_retries}) for {service_name}")
            return None
        return wrapper
    return decorator


def requests_get_with_limit(url: str, service_name: str = 'http', max_retries: int = 3, **kwargs) -> Optional[requests.Response]:
    """Wrapper around requests.get that applies rate limiting and retries"""
    rate_limiter = get_rate_limiter()
    for attempt in range(max_retries):
        try:
            rate_limiter.wait_if_needed(service_name)
            resp = requests.get(url, **kwargs)
            if getattr(resp, 'status_code', 200) == 429:
                raise requests.exceptions.HTTPError(response=resp)
            return resp
        except Exception as e:
            if is_rate_limit_error(e):
                handle_rate_limit(service_name, e, attempt, max_retries)
                continue
            raise
    logger.warning(f"✖ requests_get_with_limit: exceeded max retries ({max_retries}) for {service_name}")
    return None


def run_subprocess_with_limit(cmd: List[str], service_name: str = 'cli', max_retries: int = 3, 
                              capture_output: bool = True, text: bool = True, 
                              timeout: Optional[int] = None, stdin_input: Optional[str] = None) -> Optional[subprocess.CompletedProcess]:
    """Run subprocess.run with rate limiting and retries for external CLI tools"""
    rate_limiter = get_rate_limiter()
    for attempt in range(max_retries):
        try:
            rate_limiter.wait_if_needed(service_name)
            if stdin_input is not None:
                proc = subprocess.run(cmd, capture_output=capture_output, text=text, timeout=timeout, input=stdin_input)
            else:
                proc = subprocess.run(cmd, capture_output=capture_output, text=text, timeout=timeout)

            # If returncode indicates rate limit-like conditions, attempt retry
            stdout = proc.stdout or ''
            stderr = proc.stderr or ''
            if proc.returncode != 0 and ('rate limit' in stdout.lower() or 'rate limit' in stderr.lower() or '429' in stdout or '429' in stderr):
                raise RuntimeError(f"Subprocess rate limit or 429 detected (code {proc.returncode})")

            return proc

        except subprocess.TimeoutExpired as e:
            logger.debug(f"Subprocess timeout for {service_name}: {e}")
            if attempt < max_retries - 1:
                handle_rate_limit(service_name, e, attempt, max_retries)
                continue
            raise
        except Exception as e:
            if is_rate_limit_error(e):
                handle_rate_limit(service_name, e, attempt, max_retries)
                continue
            # Non-rate-limit failures should be returned as-is (don't swallow)
            raise
    logger.warning(f"✖ run_subprocess_with_limit: exceeded max retries ({max_retries}) for {service_name}")
    return None


rate_limiter = APIRateLimiter()


def get_rate_limiter() -> APIRateLimiter:
    return rate_limiter
