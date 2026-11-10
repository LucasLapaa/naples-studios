import os
# Somente credenciais fictícias no ambiente de testes.
os.environ['APP_BASE_URL']='http://127.0.0.1:8000'
os.environ['COOKIE_SECURE']='0'
os.environ['DISCORD_CLIENT_ID']='123456789012345678'
os.environ['DISCORD_CLIENT_SECRET']='discord-test-secret-000000000000000'
os.environ['SESSION_SECRET']='test-only-strong-secret-12345678901234567890'
os.environ['DISCORD_ALLOWED_USER_IDS']='111222333444555666'
os.environ['ROBLOX_API_KEY']='roblox-fake-test-key-0000000000'
os.environ['ROBLOX_CREATOR_ID']='999888777'
os.environ['ROBLOX_CREATOR_TYPE']='user'
os.environ['ENABLE_PLATFORM_LINKS']='1'
