# Đưa ParrotGo lên VPS Ubuntu/Debian

Nếu muốn GitHub build image trước để VPS chỉ nạp và chạy, xem [đóng gói bằng GitHub Actions](GITHUB_ACTIONS.md).

Gói này chạy một process ứng dụng và Caddy qua Docker Compose. Caddy phục vụ HTTPS, yêu cầu tài khoản beta cho cả trang và API; cổng 8000 chỉ nằm trong mạng Docker. Groq/Gemini/VietMap có thể dùng API thật, còn việc tạo/hủy đơn và giá vẫn là sandbox.

Đã chuẩn bị cấu hình và script; chưa triển khai vào VPS hoặc kiểm chứng API bằng khóa của bạn.

## 1. Chuẩn bị VPS

Domain đã trỏ về VPS: kiểm tra bản ghi A và cả AAAA nếu có. AAAA cũ trỏ sang máy khác có thể làm cấp chứng chỉ thất bại. Mở TCP 80/443 ở firewall của nhà cung cấp; giữ cổng SSH đang dùng. UDP 443 dành cho HTTP/3, có thể mở thêm. Không mở 8000.

Kiểm tra ứng dụng đang sử dụng 80/443:

```bash
sudo ss -lntp
```

Nếu Nginx/Apache hoặc một website khác đã chiếm cổng, cần điều chỉnh cấu hình proxy hiện tại trước. Script không dừng hay thay thế dịch vụ đó.

Caddy tự cấp và gia hạn chứng chỉ khi domain và kết nối đến máy chủ phù hợp. Xem [Automatic HTTPS của Caddy](https://caddyserver.com/docs/automatic-https). Docker xuất cổng có thể đi ngoài quy tắc UFW, nên kiểm soát thêm ở firewall của nhà cung cấp và xem [hướng dẫn Docker về firewall](https://docs.docker.com/engine/install/ubuntu/#firewall-limitations).

Nếu Docker Engine và plugin Compose v2 đã có, bỏ qua phần cài đặt. Nếu chưa có, dùng repository chính thức; lệnh dưới nhận diện Ubuntu/Debian, không tự gỡ package đang dùng:

```bash
. /etc/os-release
docker_os_id=$ID
case "$docker_os_id" in
  ubuntu|debian) ;;
  *) echo "Chỉ dành cho Ubuntu hoặc Debian"; exit 1 ;;
esac
docker_suite=${UBUNTU_CODENAME:-$VERSION_CODENAME}
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL "https://download.docker.com/linux/$docker_os_id/gpg" -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
printf 'Types: deb\nURIs: https://download.docker.com/linux/%s\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' \
  "$docker_os_id" "$docker_suite" "$(dpkg --print-architecture)" | sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker compose version
```

Nếu apt báo xung đột Docker/containerd hiện có, xử lý theo tài liệu cho [Ubuntu](https://docs.docker.com/engine/install/ubuntu/) hoặc [Debian](https://docs.docker.com/engine/install/debian/); không gỡ dịch vụ đang chạy khi chưa xác định tác động. Các lệnh vận hành bên dưới dùng `sudo`, không tự thay quyền tài khoản hay nhóm Docker.

## 2. Đóng gói và đưa mã nguồn lên VPS

Tại thư mục dự án trên máy Windows:

```powershell
python deploy/package.py
scp deploy/parrotgo-vps.tar.gz USER@VPS:/tmp/
ssh USER@VPS
```

Gói nguồn loại bỏ `.env`, dữ liệu, Git, `node_modules` và kết quả build của máy cá nhân. Docker sẽ build frontend và cài backend từ lockfile.

Tại VPS, lần cài đầu tiên:

```bash
sudo install -d -m 0750 /srv/parrotgo
sudo tar -xzf /tmp/parrotgo-vps.tar.gz -C /srv/parrotgo
cd /srv/parrotgo
sudo bash deploy/setup.sh
sudo nano deploy/.env
```

`setup.sh` chỉ tạo `.env` khi chưa tồn tại, giữ cấu hình cũ, sinh `APP_SECRET` và yêu cầu nhập mật khẩu beta qua terminal. Mật khẩu không hiện ký tự; script lưu bcrypt hash, không lưu mật khẩu gốc. Lệnh hash không đưa mật khẩu vào command line. Caddy yêu cầu mật khẩu đã hash; xem [basic_auth](https://caddyserver.com/docs/caddyfile/directives/basic_auth) và [hash-password](https://caddyserver.com/docs/command-line#caddy-hash-password).

Sửa các giá trị này trực tiếp trong editor trên VPS:

| Biến | Giá trị |
| --- | --- |
| `DOMAIN` | Ví dụ `chat.tenmiencuaban.vn`, không có `https://`, cổng hay dấu `/` |
| `ACME_EMAIL` | Email nhận thông báo chứng chỉ |
| `BETA_USER` | Tên đăng nhập beta, mặc định `beta` |
| `BETA_PASSWORD_HASH` | Giữ hash được sinh trong **dấu nháy đơn** |
| `APP_SECRET` | Giữ giá trị script đã sinh; cần giữ khi khôi phục dữ liệu |
| `GROQ_API_KEY` | Khóa tài khoản Groq riêng |
| `GEMINI_API_KEY` | Khóa Gemini riêng cho dự phòng |
| `VIETMAP_API_KEY` | Khóa VietMap riêng |

Các dấu `$` trong bcrypt được giữ nguyên nhờ nháy đơn, theo [quy tắc `.env` của Compose](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/#env-file-syntax). Không chạy `source deploy/.env`, không đưa khóa vào chat hoặc ảnh chụp màn hình, không chạy `docker compose config` để in toàn bộ cấu hình; dùng `config --quiet`.

Preset mặc định là `APP_PROFILE=chat_sandbox`, Groq GPT-OSS 120B chính và Gemini Flash Lite dự phòng. `AREA_ASSISTANCE_ENABLED=false` giữ dịch vụ tìm địa chỉ bên trong địa danh ở trạng thái tắt; muốn bật cần dữ liệu địa danh và chính sách đã duyệt. `WEATHER_PROVIDER=disabled` trong preset này; cần cấu hình nhà cung cấp thời tiết phù hợp trước khi bật. Các mức quota và giới hạn nhận tin trong `.env.example` là trần cục bộ, cần phù hợp tài khoản và quy mô beta.

Có thể thử khởi động trước bằng `APP_PROFILE=fixture_demo`, không cần ba khóa API. Đây là dữ liệu mẫu, không kiểm tra được tài khoản model hay VietMap. Khi chuyển sang live APIs, sửa lại `APP_PROFILE=chat_sandbox` và điền khóa riêng.

## 3. Khởi động và kiểm tra

```bash
cd /srv/parrotgo
sudo bash deploy/setup.sh --start
```

Script kiểm tra cấu hình, build và chờ healthcheck. Sau đó tạo hàm tiện dụng cho terminal hiện tại:

```bash
dc() { sudo docker compose --project-name parrotgo --env-file deploy/.env -f deploy/compose.yaml "$@"; }
dc ps
curl -I https://chat.tenmiencuaban.vn/
curl --user beta --fail https://chat.tenmiencuaban.vn/api/ready
```

Thay domain và username theo `.env`. Request HTTPS chưa đăng nhập phải trả `401`; `curl --user beta` sẽ hỏi mật khẩu mà không đưa mật khẩu vào command line. `/api/ready` trả `200` khi process, worker và database nội bộ sẵn sàng; không chứng minh khóa/quota/tài khoản API bên ngoài hoạt động.

Mở HTTPS trong trình duyệt, đăng nhập và thử:

1. Hỏi quãng đường/thời gian giữa hai địa điểm có thật; hỏi tiếp “sao đi lâu vậy”.
2. Nhập địa điểm duy nhất, xác nhận bằng văn bản; xác nhận địa điểm chưa được tạo đơn.
3. Cuộn lên xem lịch sử trong lúc có tin mới, tải lại và tiếp tục hội thoại.
4. Thử hội thoại sandbox đến bước xác nhận chuyến, rồi hủy đơn sandbox.

Nếu lỗi, kiểm tra tại VPS bằng `dc logs --tail 100 app` hoặc `dc logs --tail 100 caddy`. Không gửi toàn bộ `.env`, `docker inspect` hoặc log chứa nội dung khách/khóa ra ngoài. Khi đổi mật khẩu, hash mới phải được tạo bằng Caddy và lưu trong nháy đơn; chạy `dc up -d --force-recreate caddy`. Preset tắt Caddy admin API nên không dùng `caddy reload`.

Ứng dụng hiện chỉ hỗ trợ **một process/replica** sở hữu SQLite và coordinator. `WORKER_CONCURRENCY` là số tác vụ bên trong process; không đổi `--workers 1`, không scale service `app`.

## 4. Sao lưu và cập nhật

```bash
cd /srv/parrotgo
sudo bash deploy/backup.sh
```

Script dừng riêng `app`, giữ Caddy chạy, lưu toàn bộ volume `parrotgo_app_data` rồi khởi động lại ứng dụng qua trap, kể cả khi sao lưu lỗi. Nếu app đã dừng trước đó, script giữ trạng thái dừng. Có khoảng gián đoạn chat trong lúc sao lưu. Việc khởi động lại thất bại được báo rõ và script trả mã lỗi.

Mỗi bản sao trong `deploy/backups/` gồm:

- `*.app-data.tar.gz`: cả thư mục dữ liệu, gồm business DB, checkpoints, quota Groq/Gemini và các file SQLite WAL/SHM còn tồn tại.
- `*.env`: bản cấu hình khớp, gồm `APP_SECRET` và API keys; cần để phục hồi phiên đúng.
- `*.sha256`: checksum của hai file trên.

Thư mục backup có quyền `700`, file có quyền `600`. Nếu dùng thư mục riêng qua đối số `backup.sh`, chọn thư mục riêng ngoài Git với quyền hạn tương đương. Giữ cả bộ trên kho sao lưu riêng có mã hóa và hạn chế quyền truy cập; script không tự đẩy backup lên dịch vụ ngoài. Certificate của Caddy nằm ở hai volume Caddy riêng và không thuộc bản sao dữ liệu app này.

Trước cập nhật, sao lưu và giữ image đang chạy:

```bash
sudo bash deploy/backup.sh
sudo docker image tag parrotgo:local parrotgo:rollback-20261003
```

Sau khi đóng gói/upload mã nguồn mới, giải nén vào đúng `/srv/parrotgo`, giữ `deploy/.env` hiện có, rồi chạy lại `sudo bash deploy/setup.sh --start`. Không dùng `docker compose down -v`: lệnh đó xóa cả volume dữ liệu. Nếu cần quay lại binary, dùng image đã giữ, nhưng database sau migration có thể cần phục hồi cùng bản backup tương ứng.

## 5. Khôi phục bản sao đã kiểm tra

Khôi phục vào volume **trống** trên VPS mới là đường đi an toàn nhất. Giữ đúng phiên bản mã nguồn/image tương ứng bản sao và giữ nguyên `APP_SECRET`. Trước khi bắt đầu, kiểm tra checksum và nội dung archive từ bản backup riêng của bạn:

```bash
cd /duong-dan/backup-rieng
sha256sum -c parrotgo-THOI_DIEM-PID.sha256
tar -tzf parrotgo-THOI_DIEM-PID.app-data.tar.gz
```

Tên trên là ví dụ; thay bằng bộ file thực tế cùng timestamp/PID. Không dùng archive không rõ nguồn hoặc checksum sai.

Tại thư mục `/srv/parrotgo`, bảo đảm `app` đã dừng và chọn đúng volume. Nếu VPS đang có dữ liệu, tạo một bản sao mới trước; không trộn bản cũ với dữ liệu hiện tại và không xóa volume để “thử lại”. Cần một phiên bảo trì có kế hoạch khi phục hồi đè vào VPS đang dùng.

```bash
cd /srv/parrotgo
sudo install -m 0600 /duong-dan/backup-rieng/parrotgo-THOI_DIEM-PID.env deploy/.env
sudo nano deploy/.env
dc() { sudo docker compose --project-name parrotgo --env-file deploy/.env -f deploy/compose.yaml "$@"; }
dc stop app
sudo docker volume inspect parrotgo_app_data --format '{{.Name}}'
```

File `.env` ở trên phải thuộc cùng bộ backup. Chỉ sửa domain/email nếu chuyển host; giữ `APP_SECRET`. Đối với VPS đã có dữ liệu, hoàn thành bản sao mới trước khi thay `.env` hiện có.

Trên VPS mới, nếu volume chưa tồn tại, tạo volume đúng tên:

```bash
sudo docker volume create --label com.docker.compose.project=parrotgo --label com.docker.compose.volume=app_data parrotgo_app_data
```

Lệnh nhập bên dưới kiểm tra volume trống trước khi giải nén, không xóa dữ liệu sẵn có:

```bash
backup_dir=$(realpath /duong-dan/backup-rieng)
archive=parrotgo-THOI_DIEM-PID.app-data.tar.gz
sudo docker run --rm --network none \
  --mount type=volume,src=parrotgo_app_data,dst=/restore \
  --mount "type=bind,src=$backup_dir,dst=/backup,readonly" \
  alpine:3.22 sh -c 'test -z "$(find /restore -mindepth 1 -print -quit)" || { echo "Volume is not empty; restore stopped" >&2; exit 1; }; tar -xzf "/backup/$1" -C /restore' sh "$archive"
```

Kiểm tra SQLite ở chế độ chỉ đọc, với volume vẫn chưa có process app ghi:

```bash
dc build app
sudo docker run --rm --network none --entrypoint python \
  --mount type=volume,src=parrotgo_app_data,dst=/restore,readonly \
  parrotgo:local -c 'import pathlib,sqlite3; files=list(pathlib.Path("/restore").glob("*.sqlite")); assert files,"No SQLite databases found"; checks=[(p.name,sqlite3.connect(p.as_uri()+"?mode=ro",uri=True).execute("PRAGMA integrity_check").fetchone()[0]) for p in files]; print(checks); assert all(result=="ok" for _,result in checks)'
```

`dc build app` chuẩn bị image trên VPS mới và không khởi động process app. Sau khi validation đạt, chạy `sudo bash deploy/setup.sh --start`, kiểm tra HTTPS, `/api/ready`, lịch sử và đơn sandbox. Nếu validation không đạt, giữ app dừng và điều tra trước khi thay đổi dữ liệu.

## Kiểm chứng và giới hạn

Tại máy phát triển đã đạt 563 kiểm thử backend, kiểm tra Ruff/schema, build frontend và kiểm tra cú pháp Bash của hai script. Máy Windows hiện không có Docker runtime và chưa có thông tin truy cập VPS để triển khai. Build Docker, backup/restore volume thật, cấp TLS và kết nối qua domain chưa được kiểm chứng trên máy chủ; cần hoàn thành các bước smoke ở trên trước khi mời khách beta.
