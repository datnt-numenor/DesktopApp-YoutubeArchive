# DesktopApp-YoutubeArchive

Ứng dụng desktop chạy cục bộ để lưu metadata playlist YouTube, phát và tải
nhạc MP3 về máy cá nhân. Giao diện và các luồng nghiệp vụ được kế thừa từ
YouTube Playlist Archiver, nhưng bản này không cần PostgreSQL, Redis, Celery,
S3/R2, Docker hay Cloudflare Tunnel.

## Chức năng

- Thêm playlist YouTube và lưu metadata vào SQLite.
- Tạo playlist thủ công từ bài đã tải, file MP3 trong máy hoặc link video YouTube riêng lẻ.
- Đồng bộ để phát hiện video mới hoặc video không còn trên YouTube; metadata và thumbnail cũ vẫn được giữ lại.
- Phân biệt video thật sự bị xóa/private với video vẫn hoạt động nhưng đã được gỡ khỏi playlist nguồn.
- Tải và lưu trữ MP3 bằng `yt-dlp` và FFmpeg.
- Tìm kiếm, phát media, repeat, shuffle và tự chuyển bài.
- Tìm playlist theo tên playlist, tên bài, kênh hoặc YouTube video ID.
- Xóa mềm và khôi phục từng bài mà không xóa database hoặc file MP3.
- Bật/tắt tự đồng bộ cho từng playlist YouTube; playlist thủ công không tham gia lịch tự động.
- Đồng bộ và tải bài mới khi ứng dụng đang đóng bằng Windows Task Scheduler.
- Trang History lưu 30 ngày lịch sử Task Scheduler, có lọc theo ngày, playlist và kết quả.
- Hiển thị tiến trình tải, lỗi từng video và dung lượng đã sử dụng.
- Giới hạn mặc định 50 playlist và 20 GB nhạc, cảnh báo khi dùng từ 80% và 95%.
- Bảo vệ bằng một mật khẩu duy nhất; không cần email hoặc tên đăng nhập.
- Đổi mật khẩu trong Settings sau khi xác nhận mật khẩu hiện tại.

## Dữ liệu cục bộ

Mặc định, toàn bộ dữ liệu riêng của ứng dụng nằm trong:

```text
data/
|-- ytarchive.db
|-- tools/yt-dlp.exe
`-- downloads/
```

Thư mục này được Git bỏ qua và hoàn toàn tách khỏi project `YoutubeArchive`.
Có thể đổi vị trí lưu bằng biến môi trường `YTARCHIVE_DESKTOP_DATA_DIR`.

## Yêu cầu trên Windows

- Python 3.10 trở lên.
- FFmpeg và `ffprobe` phải có trong `PATH` để tải và đọc metadata MP3.
- Node.js 22 trở lên phải có trong `PATH` để yt-dlp giải JavaScript challenge của YouTube.

Ứng dụng tự tải `yt-dlp.exe` chính thức vào `data/tools`, xác minh SHA-256 và kiểm tra
bản stable mới mỗi tuần. Nếu cập nhật thất bại, ứng dụng tiếp tục dùng bản đang hoạt động;
bản trước đó được giữ lại để rollback. Các file công cụ này là dữ liệu cục bộ và không được commit lên Git.

Cài FFmpeg bằng Chocolatey:

```powershell
choco install ffmpeg -y
ffmpeg -version
ffprobe -version
```

Sau đó chạy PowerShell trong thư mục project:

```powershell
.\run-desktop.ps1
```

Lần chạy đầu script sẽ tạo `.venv`, cài dependencies, sao chép `.env.example`
thành `.env`, sau đó yêu cầu đặt mật khẩu và mở ứng dụng trong một cửa sổ desktop. Web server nội bộ
chỉ lắng nghe trên `127.0.0.1`; máy khác trong mạng không thể truy cập.

Nếu Windows chặn PowerShell script, có thể chạy thủ công:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe desktop.py
```

## Đồng bộ tự động khi ứng dụng đóng

Sau khi đã chạy ứng dụng ít nhất một lần và tạo `.venv`, cài lịch chạy hằng ngày:

```powershell
.\scripts\install-scheduled-sync.ps1 -Time "12:00"
```

Task Scheduler chỉ xử lý những playlist YouTube đang bật Auto-sync và luôn tải MP3.
Playlist thủ công không được đồng bộ với nguồn bên ngoài. Có thể đổi giờ trực tiếp
trong Settings hoặc gỡ lịch bằng:

```powershell
.\scripts\uninstall-scheduled-sync.ps1
```

## Kiểm thử

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

Kiểm tra server thực tế mà không mở cửa sổ GUI:

```powershell
.\.venv\Scripts\python.exe scripts\smoke_desktop.py
```

## Ghi chú

- Kết nối Internet vẫn cần thiết để đọc playlist và tải video từ YouTube.
- Ứng dụng chỉ mở local; không có cấu hình deploy public trong project này.
- `.env`, database, MP3, thumbnail và backup nằm ngoài Git; `.env.example` chỉ chứa cấu hình mẫu.
- Không sao chép dữ liệu cá nhân hoặc lịch sử Git từ project gốc.
