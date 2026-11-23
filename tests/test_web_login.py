import unittest
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.api import app
from app.auth import session_token, valid_session
from app.auth import password_hash
pytestmark = pytest.mark.usefixtures('auth_memory')


class WebLoginTests(unittest.TestCase):
    def test_login_is_in_page_and_api_never_challenges_browser(self):
        config={'host':'0.0.0.0','browser_user':'family','browser_password_hash':password_hash('test-password'),'auth_secret':'fixture-secret-'*4}
        with patch('app.auth.api.load_config',return_value=config):
            client=TestClient(app)
            response=client.get('/',follow_redirects=False)
            self.assertEqual(response.status_code,303)
            self.assertEqual(response.headers['location'],'/login')
            self.assertEqual(client.get('/login').status_code,200)
            response=client.get('/api/status')
            self.assertEqual(response.status_code,401)
            self.assertNotIn('www-authenticate',response.headers)
            self.assertEqual(client.post('/api/login',json={'username':'family','password':'wrong'}).status_code,401)
            response=client.post('/api/login',json={'username':'family','password':'test-password'})
            self.assertEqual(response.status_code,200)
            self.assertIn('HttpOnly',response.headers['set-cookie'])
            self.assertEqual(client.get('/').status_code,200)
            config['browser_password_hash']=password_hash('changed')
            self.assertEqual(client.get('/api/status').status_code,401)

    def test_expired_and_modified_tokens_are_rejected(self):
        config={'browser_user':'family','browser_password_hash':password_hash('test-password'),'auth_secret':'fixture-secret-'*4}
        self.assertFalse(valid_session(config,session_token(config,1)))
        self.assertFalse(valid_session(config,'invalid'))
        self.assertFalse(valid_session(config,'9999999999.bad'))
