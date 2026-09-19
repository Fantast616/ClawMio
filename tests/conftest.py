"""Offline test defaults, established before application modules load .env."""
import os

os.environ.update({
    'DASHSCOPE_API_KEY':'offline-test-key',
    'BAILIAN_WORKSPACE_ID':'ws_test',
    'BAILIAN_REGION':'cn-beijing',
    'ADMIN_PASSWORD':'offline-test-password',
    'COOKIE_SECRET':'offline-test-cookie-secret',
    'DEFAULT_AGENT_ID':'',
    'DEFAULT_ENVIRONMENT_ID':'',
    'PUBLIC_BASE_URL':'http://testserver',
    'CLAW_API_BASE_URL':'https://bots.example.com',
})
