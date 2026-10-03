import { defineConfig } from '@playwright/test'
import path from 'node:path'

const backend = path.resolve('../backend')
const python = path.join(backend, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
export default defineConfig({
  testDir: './tests', timeout: 45000, fullyParallel: false, workers: 1,
  use: { baseURL: 'http://127.0.0.1:8001', headless: true },
  webServer: {
    command: `"${python}" -m uvicorn app.main:app --app-dir "${backend}" --host 127.0.0.1 --port 8001 --workers 1`,
    url: 'http://127.0.0.1:8001/api/ready', reuseExistingServer: false,
    env: {
      APP_PROFILE: 'test', APP_SECRET: 'e2e-stable-secret',
      BUSINESS_DB_PATH: path.resolve('../../.cache/e2e/app.sqlite'),
      CHECKPOINT_DB_PATH: path.resolve('../../.cache/e2e/checkpoints.sqlite'),
      ALLOWED_ORIGINS: 'http://127.0.0.1:8001',
      AREA_ASSISTANCE_ENABLED: 'true',
    },
  },
})
