"""Create an ignored local configuration without overwriting credentials."""
import secrets
import sys
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

root = Path(__file__).resolve().parents[1]
target = root / 'backend/.env'
if target.exists():
    print('Giữ nguyên src/backend/.env hiện có.')
else:
    content = (root / 'backend/.env.example').read_text(encoding='utf-8')
    content = content.replace('APP_SECRET=\n', 'APP_SECRET=' + secrets.token_urlsafe(48) + '\n')
    target.write_text(content, encoding='utf-8')
    print('Đã tạo src/backend/.env. Điền GROQ_API_KEY, GEMINI_API_KEY dự phòng và VIETMAP_API_KEY để chạy chat_sandbox; hoặc đặt LLM_PROVIDER=gemini để dùng cấu hình cũ.')
