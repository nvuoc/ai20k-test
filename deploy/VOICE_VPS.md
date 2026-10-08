# Đưa bản đặt xe bằng giọng nói lên VPS

Một image chứa frontend, API và LiveKit agent với Azure STT/TTS. Compose chạy `app`, `voice-agent` và Caddy. API dùng một process sở hữu SQLite; agent gọi `http://app:8000` trong mạng Docker và không cần khóa Groq/Gemini/VietMap. LiveKit Cloud đảm nhiệm kết nối âm thanh. Caddy chỉ xuất 80/443; không mở 8000/8081. API của worker bị chặn ở proxy công khai.

## Build trên GitHub

Commit cả `src/backend/requirements.voice.lock`, `uv.lock`, frontend lockfile, Dockerfile và thư mục `deploy`. Không commit `.env`, database hay thư mục `.venv`. Không cần đưa API key vào GitHub Secrets: workflow chỉ dùng `GITHUB_TOKEN` để đẩy GHCR.

Push lên `main`/`master`, hoặc chạy **Actions → Package VPS → Run workflow**. Mặc định build amd64; chọn arm64 nếu `uname -m` trên VPS là `aarch64`. Workflow chạy backend tests, browser tests, build image, kiểm tra native Azure/Silero không cần key và smoke test API/SQLite trước khi publish. Pull request chỉ kiểm thử, không publish.

Run thành công cung cấp:

- Image `ghcr.io/<owner>/<repo>:<commit-sha>-amd64` hoặc `-arm64`; tên owner/repo viết thường. `latest-amd64`/`latest-arm64` chỉ cập nhật trên default branch.
- Artifact `parrotgo-vps-<arch>-<commit>` chứa source bundle, Docker image archive, thông tin build và checksum. Có thể dùng artifact khi không muốn đăng nhập GHCR trên VPS.

Lần build Linux thực tế được kiểm chứng bởi run GitHub Actions; máy Windows hiện tại chưa có Docker để chạy image tại chỗ.

## Cài lần đầu từ artifact

Cài Docker Engine và Compose v2 theo [hướng dẫn VPS](README.md). Giải nén ZIP artifact trên máy của bạn, rồi tại thư mục đó:

```powershell
scp parrotgo-vps.tar.gz parrotgo-image.tar.gz BUILD_INFO.txt SHA256SUMS USER@VPS:/tmp/
ssh USER@VPS
```

Trên VPS:

```bash
cd /tmp
sha256sum -c SHA256SUMS
sudo install -d -m 0750 /srv/parrotgo
sudo tar -xzf parrotgo-vps.tar.gz -C /srv/parrotgo
sudo docker load -i /tmp/parrotgo-image.tar.gz
cd /srv/parrotgo
sudo bash deploy/setup.sh
sudo nano deploy/.env
```

Điền cấu hình trực tiếp trong editor; không dán key vào câu lệnh shell:

| Biến | Giá trị |
| --- | --- |
| `DOMAIN`, `ACME_EMAIL`, `BETA_USER` | Domain trỏ tới VPS, email TLS, tài khoản beta |
| `APP_SECRET`, `BETA_PASSWORD_HASH` | Giữ giá trị setup sinh, hash nằm trong nháy đơn |
| `PARROTGO_IMAGE` | `parrotgo:local` khi dùng image archive |
| `VOICE_ENABLED` | `true` |
| `LIVEKIT_URL` | `wss://<project>.livekit.cloud` của project |
| `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | Cặp key/secret của cùng project LiveKit |
| `LIVEKIT_AGENT_NAME` | `parrotgo-booking`, phải giống nhau giữa API và agent |
| `VOICE_AGENT_SECRET` | Setup sinh tự động; API và agent phải dùng cùng giá trị |
| `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION` | Key Speech resource và đúng region, ví dụ `southeastasia` |
| `AZURE_SPEECH_VOICE` | `vi-VN-HoaiMyNeural` |
| `AZURE_TTS_ENDPOINT` | Để trống; agent tự tạo synthesis URL từ region |
| `GROQ_API_KEY`, `GEMINI_API_KEY`, `VIETMAP_API_KEY` | Key cho lõi đặt xe theo provider/fallback đang bật |

Không sao chép đè toàn bộ `.env` trên VPS bằng `.env` ở Windows: cấu hình local có URL và đường dẫn khác, đồng thời phải giữ `APP_SECRET` của VPS. Setup giữ key đã có, sinh secret còn thiếu và kiểm tra cấu hình trước khi chạy.

```bash
sudo chmod 600 deploy/.env
sudo bash deploy/setup.sh --start --image
dc() { sudo docker compose --project-name parrotgo --env-file deploy/.env -f deploy/compose.yaml --profile voice "$@"; }
dc ps
dc logs --tail 80 voice-agent
```

Cần VPS có đủ RAM cho giới hạn cấu hình: API tối đa 1 GiB, voice agent 2 GiB, Caddy 256 MiB, cộng hệ điều hành. Không scale `app` hoặc tăng Uvicorn workers khi còn dùng SQLite/coordinator hiện tại.

Để kiểm tra offline không dùng API thật: `APP_PROFILE=fixture_demo`, `VOICE_ENABLED=false`, rồi chạy setup. Chế độ này không khởi động agent và không kiểm tra Speech/LiveKit.

## Dùng image từ GHCR

Với package public, chỉ cần `docker pull`. Với package private, đăng nhập bằng GitHub PAT classic có `read:packages` và quyền truy cập package; nhập token tại lời nhắc password của Docker, không đưa token vào command line. Xem [hướng dẫn GHCR](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

```bash
sudo docker login ghcr.io -u YOUR_GITHUB_USER
sudo docker pull ghcr.io/owner/repo:COMMIT_SHA-amd64
sudo nano /srv/parrotgo/deploy/.env
```

Đặt `PARROTGO_IMAGE=ghcr.io/owner/repo:COMMIT_SHA-amd64` trong editor, thay bằng đúng image/tag từ run. Source/config Compose trên VPS phải là cùng bản commit (lấy source bundle của run). Sau đó:

```bash
cd /srv/parrotgo
sudo bash deploy/setup.sh --start --image
```

## Thay API key mới trên VPS đang chạy

Thực hiện qua SSH, không cần rebuild image hoặc push key lên GitHub:

```bash
ssh USER@VPS
cd /srv/parrotgo
sudo nano deploy/.env
```

Thay các key cần đổi. Với LiveKit, thay cả `LIVEKIT_API_KEY` và `LIVEKIT_API_SECRET`; URL phải thuộc cùng project. Với Azure, thay `AZURE_SPEECH_KEY` và kiểm tra `AZURE_SPEECH_REGION` đúng resource. Nếu VPS cũ chưa có các biến voice, thêm các biến trong bảng trên; để `VOICE_AGENT_SECRET=` trống cho setup sinh. Giữ nguyên `APP_SECRET`, beta hash và các key không đổi.

```bash
sudo chmod 600 deploy/.env
sudo bash deploy/setup.sh --start --image
dc() { sudo docker compose --project-name parrotgo --env-file deploy/.env -f deploy/compose.yaml --profile voice "$@"; }
dc up -d --no-build --force-recreate --wait --wait-timeout 180 app voice-agent
dc ps
```

`restart` đơn thuần không đọc lại environment của container; `up --force-recreate` tạo container với key mới. Thay key/recreate làm ngắt cuộc gọi đang diễn ra, khách cần kết nối lại. Dữ liệu SQLite vẫn trong named volume; không chạy `down -v`.

Mở HTTPS, đăng nhập beta, nhập tên/số điện thoại, cấp quyền micro và thử nghe bot, nói điểm đón/đến, chọn “một”/“hai” hoặc nói tên địa điểm. Bot chỉ dùng hai kết quả VietMap đầu, yêu cầu nói rõ nếu chưa phân biệt được. Healthcheck agent xác nhận kết nối LiveKit; một cuộc gọi thật mới kiểm chứng Azure key/quota và âm thanh trình duyệt. Sau khi key mới hoạt động, thu hồi key cũ tại nhà cung cấp.

Nếu lỗi: kiểm tra `dc logs --tail 80 app voice-agent`. Không in toàn bộ `.env`, `docker inspect` hoặc `docker compose config`; dùng `config --quiet` khi cần kiểm tra cấu hình. Không cần tự triển khai LiveKit server trên VPS này.
