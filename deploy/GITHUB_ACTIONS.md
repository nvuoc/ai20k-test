# Đóng gói VPS bằng GitHub Actions

Workflow kiểm thử backend và trình duyệt, build image chứa frontend/API/LiveKit agent/Azure Speech, kiểm tra native runtime rồi chạy smoke test với dữ liệu mẫu. Sau khi các bước thành công, workflow đẩy image lên GHCR và xuất gói tải về. VPS chỉ cần Docker Engine và Compose để nạp/chạy image. Workflow không SSH vào VPS.

## 1. Đưa đúng cấu trúc lên repository

Giữ các thư mục ở cấp gốc, không đưa cả dự án vào một thư mục con khác:

```text
Dockerfile
.dockerignore
.github/workflows/package.yml
src/backend/app/
src/backend/requirements.runtime.lock
src/frontend/
deploy/
```

Giữ cả lockfile và cấu hình được theo dõi trong Git. `.env`, dữ liệu SQLite, backup, `node_modules` và archive đã tạo không được commit. Các khóa Groq/Gemini/VietMap và thông tin SSH không cần đưa vào GitHub Secrets: chúng chỉ nằm trong `deploy/.env` trên VPS.

Hướng dẫn dùng GHCR, cấu hình voice và thay API key trên VPS: [VOICE_VPS.md](VOICE_VPS.md). Tag GHCR có dạng `ghcr.io/<owner>/<repo>:<commit>-amd64` hoặc `-arm64`; `latest-amd64`/`latest-arm64` chỉ cập nhật trên default branch.

## 2. Chạy và tải artifact

Đưa workflow lên default branch. Trong repository, vào **Actions → Package VPS → Run workflow**. Workflow cũng chạy khi push vào `main`/`master`, push tag `v*` và pull request vào hai nhánh này khi mã nguồn/cấu hình liên quan thay đổi; gói triển khai lấy từ run thành công của nhánh/phát hành bạn đã duyệt.

Mặc định image là `linux/amd64`, phù hợp VPS x86_64. Kiểm tra trên VPS bằng `uname -m`: `x86_64` tương ứng amd64, `aarch64` tương ứng arm64. Khi chạy thủ công, chọn input **platform = linux/arm64** nếu VPS dùng ARM. Build ARM sử dụng QEMU nên có thể lâu hơn; push/PR tự động tạo bản amd64.

Mở run thành công, tải artifact `parrotgo-vps-<amd64|arm64>-<commit>` trong phần **Artifacts** và giải nén ZIP. Workflow đặt thời gian giữ artifact 14 ngày, trong giới hạn retention của repository. Cần đăng nhập GitHub bằng tài khoản có quyền đọc repository để tải. [Hướng dẫn tải artifact của GitHub](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts).

Bộ file gồm:

| File | Nội dung |
| --- | --- |
| `parrotgo-vps.tar.gz` | Mã nguồn và cấu hình triển khai, không có khóa/dữ liệu riêng |
| `parrotgo-image.tar.gz` | Docker image đã build, gồm frontend và backend |
| `BUILD_INFO.txt` | Commit, kiến trúc, mã run và tag image |
| `SHA256SUMS` | Checksum của hai archive và BUILD_INFO.txt |

## 3. Nạp và chạy trên VPS

Tại thư mục artifact đã giải nén trên Windows:

```powershell
scp parrotgo-vps.tar.gz parrotgo-image.tar.gz BUILD_INFO.txt SHA256SUMS USER@VPS:/tmp/
ssh USER@VPS
```

Lần cài đầu trên VPS:

```bash
cd /tmp
sha256sum -c SHA256SUMS
sudo install -d -m 0750 /srv/parrotgo
sudo tar -xzf parrotgo-vps.tar.gz -C /srv/parrotgo
sudo docker load -i /tmp/parrotgo-image.tar.gz
cd /srv/parrotgo
sudo bash deploy/setup.sh
sudo nano deploy/.env
sudo bash deploy/setup.sh --start --image
```

`docker load` nạp cả image và tag từ gzip archive. [Tài liệu Docker](https://docs.docker.com/reference/cli/docker/image/load/). `--image` dùng image đã nạp, bỏ qua build trên VPS. Điền domain/email, tài khoản beta và API keys riêng trong editor; giữ `APP_SECRET` và bcrypt hash do setup sinh. Voice cần LiveKit/Azure keys theo [VOICE_VPS.md](VOICE_VPS.md). Có thể dùng `APP_PROFILE=fixture_demo` và `VOICE_ENABLED=false` để thử offline.

Khi cập nhật VPS đang chạy, thực hiện `sudo bash deploy/backup.sh` và giữ image cũ **trước** khi nạp image mới. Giữ `deploy/.env` hiện có; dữ liệu nằm trong named volume, không dùng `docker compose down -v`. Xem [hướng dẫn VPS](README.md) để cài Docker, cấu hình DNS/HTTPS, kiểm tra Basic Auth và backup/restore.

## Phạm vi kiểm chứng

Smoke test fixture xác nhận image có thể khởi động với dữ liệu mẫu, không xác nhận khóa/quota API thật, TLS hoặc VPS của bạn. Workflow đã được chuẩn bị trong mã nguồn; chỉ có thể khẳng định run thành công sau khi chạy trên GitHub. Máy phát triển hiện chưa có Docker runtime để chạy image tại chỗ.
