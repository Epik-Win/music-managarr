"""Test suite for Lidarr artist monitoring/unmonitoring functionality."""

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import sys
import types
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

# Mock dependencies
if 'plexapi' not in sys.modules:
    plexapi = types.ModuleType('plexapi')
    sys.modules['plexapi'] = plexapi
else:
    plexapi = sys.modules['plexapi']

plexapi_server = types.ModuleType('plexapi.server')
plexapi_server.PlexServer = type('PlexServer', (), {})
sys.modules['plexapi.server'] = plexapi_server
setattr(plexapi, 'server', plexapi_server)

if 'numpy' not in sys.modules:
    numpy = types.ModuleType('numpy')
    numpy.array = lambda *args, **kwargs: args[0]
    numpy.mean = lambda x: sum(x) / len(x) if x else 0
    sys.modules['numpy'] = numpy

for module_name in ['sklearn', 'sklearn.preprocessing', 'sklearn.neighbors']:
    if module_name not in sys.modules:
        sys.modules[module_name] = types.ModuleType(module_name)

if 'sklearn.preprocessing' not in sys.modules:
    sys.modules['sklearn.preprocessing'] = types.ModuleType('sklearn.preprocessing')
if 'sklearn.neighbors' not in sys.modules:
    sys.modules['sklearn.neighbors'] = types.ModuleType('sklearn.neighbors')
setattr(sys.modules['sklearn.preprocessing'], 'StandardScaler', type('StandardScaler', (), {'fit_transform': lambda self, x: x}))
setattr(sys.modules['sklearn.neighbors'], 'NearestNeighbors', type('NearestNeighbors', (), {'fit': lambda self, x: None, 'kneighbors': lambda self, x, n_neighbors=1: ([], [])}))


import pytest
import app


class TestLidarrUnmonitor:
    """Test the Lidarr artist unmonitoring (removal from top-5) logic."""

    @pytest.fixture
    def setup_test_db(self):
        """Create a temporary test database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / 'test.db'
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            
            # Create the table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS lidarr_followed_artists (
                    artist_name TEXT PRIMARY KEY,
                    foreign_artist_id TEXT,
                    lidarr_artist_id INTEGER,
                    artist_score REAL,
                    recent_listen_count INTEGER,
                    followed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()
            conn.close()
            yield str(db_path)

    @pytest.fixture
    def mock_app_instance(self, setup_test_db):
        """Create a mock app instance with test database."""
        with patch('app.DB_PATH', setup_test_db):
            with patch('app.LIDARR_URLS', ['http://localhost:8686']):
                with patch('app.LIDARR_API_KEYS', ['test-api-key']):
                    mock_instance = Mock(spec=app.MusicAIAnalyzer)
                    # Bind the real methods to the mock
                    mock_instance._lidarr_unmonitor_artist = app.MusicAIAnalyzer._lidarr_unmonitor_artist.__get__(mock_instance)
                    yield mock_instance

    def test_unmonitor_artist_with_lidarr_id(self, mock_app_instance):
        """Test unmonitoring an artist using lidarr_artist_id."""
        artist_name = "Test Artist"
        lidarr_artist_id = 123
        foreign_artist_id = "abc123"
        lidarr_url = "http://localhost:8686"
        headers = {'X-Api-Key': 'test-key'}
        
        # Mock the GET request (fetch current artist)
        mock_get_response = Mock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = {
            'id': lidarr_artist_id,
            'name': artist_name,
            'monitored': True,
            'monitorNewItems': 'all'
        }
        
        # Mock the PUT request (update artist to unmonitored)
        mock_put_response = Mock()
        mock_put_response.status_code = 200
        
        with patch('app.requests.get', return_value=mock_get_response):
            with patch('app.requests.put', return_value=mock_put_response) as mock_put:
                result = mock_app_instance._lidarr_unmonitor_artist(
                    artist_name, lidarr_artist_id, foreign_artist_id, lidarr_url, headers
                )
                
                # Verify result is True
                assert result is True
                
                # Verify PUT was called with monitored=False and monitorNewItems=none
                mock_put.assert_called_once()
                call_args = mock_put.call_args
                assert call_args[1]['json']['monitored'] is False
                assert call_args[1]['json']['monitorNewItems'] == 'none'

    def test_unmonitor_artist_resolves_from_foreign_id(self, mock_app_instance):
        """Test unmonitoring using foreign_artist_id when lidarr_id is missing."""
        artist_name = "Another Artist"
        lidarr_artist_id = None
        foreign_artist_id = "xyz789"
        lidarr_url = "http://localhost:8686"
        headers = {'X-Api-Key': 'test-key'}
        
        # Mock the GET requests
        mock_get_foreign = Mock()
        mock_get_foreign.status_code = 200
        mock_get_foreign.json.return_value = {'id': 456}  # Resolve foreign ID to lidarr ID
        
        mock_get_artist = Mock()
        mock_get_artist.status_code = 200
        mock_get_artist.json.return_value = {
            'id': 456,
            'name': artist_name,
            'monitored': True,
            'monitorNewItems': 'all'
        }
        
        mock_put_response = Mock()
        mock_put_response.status_code = 200
        
        get_responses = [mock_get_foreign, mock_get_artist]
        
        with patch('app.requests.get', side_effect=get_responses):
            with patch('app.requests.put', return_value=mock_put_response):
                result = mock_app_instance._lidarr_unmonitor_artist(
                    artist_name, lidarr_artist_id, foreign_artist_id, lidarr_url, headers
                )
                
                # Verify result is True
                assert result is True

    def test_unmonitor_artist_fails_without_ids(self, mock_app_instance):
        """Test unmonitoring fails gracefully when neither ID is available."""
        artist_name = "Unknown Artist"
        lidarr_artist_id = None
        foreign_artist_id = None
        lidarr_url = "http://localhost:8686"
        headers = {'X-Api-Key': 'test-key'}
        
        result = mock_app_instance._lidarr_unmonitor_artist(
            artist_name, lidarr_artist_id, foreign_artist_id, lidarr_url, headers
        )
        
        # Verify result is False (failure)
        assert result is False

    def test_unmonitor_artist_handles_put_failure(self, mock_app_instance):
        """Test unmonitoring handles PUT request failure gracefully."""
        artist_name = "Failed Artist"
        lidarr_artist_id = 789
        foreign_artist_id = "def456"
        lidarr_url = "http://localhost:8686"
        headers = {'X-Api-Key': 'test-key'}
        
        mock_get_response = Mock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = {
            'id': 789,
            'name': artist_name,
            'monitored': True,
            'monitorNewItems': 'all'
        }
        
        # Simulate PUT failure (e.g., 500 error)
        mock_put_response = Mock()
        mock_put_response.status_code = 500
        mock_put_response.text = "Internal Server Error"
        
        with patch('app.requests.get', return_value=mock_get_response):
            with patch('app.requests.put', return_value=mock_put_response):
                result = mock_app_instance._lidarr_unmonitor_artist(
                    artist_name, lidarr_artist_id, foreign_artist_id, lidarr_url, headers
                )
                
                # Verify result is False (failure)
                assert result is False

    def test_sync_rotation_evicts_fallen_artists(self, setup_test_db):
        """Test that sync_top_artists_to_lidarr correctly evicts artists that fall out of top-5."""
        # Setup: Insert 3 artists into followed_artists table
        conn = sqlite3.connect(setup_test_db)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO lidarr_followed_artists
            (artist_name, foreign_artist_id, lidarr_artist_id, artist_score, recent_listen_count)
            VALUES
            (?, ?, ?, ?, ?),
            (?, ?, ?, ?, ?),
            (?, ?, ?, ?, ?)
        ''', [
            'Old Artist 1', 'foreign1', 100, 0.7, 10,
            'Old Artist 2', 'foreign2', 101, 0.6, 8,
            'Current Artist', 'foreign3', 102, 0.8, 15
        ])
        conn.commit()
        conn.close()
        
        # Create real instance and mock specific methods
        with patch('app.DB_PATH', setup_test_db):
            with patch('app.LIDARR_URLS', ['http://localhost:8686']):
                with patch('app.LIDARR_API_KEYS', ['test-key']):
                    with patch('app.logger'):  # Mock logger to avoid output
                        app_instance = app.MusicAIAnalyzer()
                        
                        # Mock only the methods we want to control
                        app_instance.get_recency_weighted_top_artists = Mock(return_value=[
                            ('Current Artist', 15, 0, 0.8, 0),
                            ('New Artist', 12, 0, 0.75, 0)
                        ])
                        
                        app_instance._lidarr_find_artist = Mock(return_value={
                            'foreignArtistId': 'foreign_new',
                            'name': 'New Artist'
                        })
                        
                        app_instance._lidarr_add_or_monitor_artist = Mock(return_value=('foreign_new', 103))
                        app_instance._lidarr_unmonitor_artist = Mock(return_value=True)
                        
                        result = app_instance.sync_top_artists_to_lidarr(top_n=2, user_index=0)
        
        # Verify stats
        assert result['evicted'] >= 1, "At least one artist should be evicted"
        assert result['added'] >= 1, "At least one artist should be added"
        
        # Verify unmonitor was called for evicted artists
        # (We mocked it, so check the call count)
        assert app_instance._lidarr_unmonitor_artist.called

    def test_evicted_artist_removed_from_database(self, setup_test_db):
        """Test that evicted artists are removed from lidarr_followed_artists table."""
        # Setup: Insert 2 artists
        conn = sqlite3.connect(setup_test_db)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO lidarr_followed_artists
            (artist_name, foreign_artist_id, lidarr_artist_id, artist_score, recent_listen_count)
            VALUES (?, ?, ?, ?, ?), (?, ?, ?, ?, ?)
        ''', [
            'To Evict', 'foreign_evict', 200, 0.5, 5,
            'To Keep', 'foreign_keep', 201, 0.9, 20
        ])
        conn.commit()
        conn.close()
        
        # Create real instance and mock specific methods
        with patch('app.DB_PATH', setup_test_db):
            with patch('app.LIDARR_URLS', ['http://localhost:8686']):
                with patch('app.LIDARR_API_KEYS', ['test-key']):
                    with patch('app.logger'):  # Mock logger to avoid output
                        app_instance = app.MusicAIAnalyzer()
                        
                        # Mock methods
                        app_instance.get_recency_weighted_top_artists = Mock(return_value=[
                            ('To Keep', 20, 0, 0.9, 0)
                        ])
                        app_instance._lidarr_unmonitor_artist = Mock(return_value=True)
                        
                        result = app_instance.sync_top_artists_to_lidarr(top_n=1, user_index=0)
        
        # Verify artist was evicted
        assert result['evicted'] == 1, "One artist should be evicted"
        
        # Verify evicted artist is no longer in database
        conn = sqlite3.connect(setup_test_db)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM lidarr_followed_artists WHERE artist_name = ?', ('To Evict',))
        count = cursor.fetchone()[0]
        conn.close()
        
        assert count == 0, "Evicted artist should be removed from database"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
