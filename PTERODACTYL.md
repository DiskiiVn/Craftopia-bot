# Triển khai Craftopia Bot lên Pterodactyl

## 1. Tạo server Python

- Chọn egg **Python Generic** và image **Python 3.12**.
- Không dùng nhạc: tối thiểu 512 MB RAM, 500 MB dung lượng và khoảng 25% CPU. Khi bật Craftopia Music, nên cấp khoảng 768 MB RAM và 50% CPU trở lên cho một luồng phát ổn định.
- Bot không mở web server nên không cần dùng allocation/cổng inbound.

Egg Python chính thức: [pterodactyl/generic-eggs](https://github.com/pterodactyl/generic-eggs/blob/main/python/egg-python-generic.json).

## 2. Upload gói Pterodactyl

1. Dừng server trên Panel.
2. Vào **Files**, upload ZIP dành cho Pterodactyl.
3. Bấm **Unarchive/Decompress** ngay tại `/home/container`.
4. Kiểm tra các đường dẫn sau tồn tại trực tiếp:

```text
/home/container/bot.py
/home/container/music.py
/home/container/requirements.txt
/home/container/knowledge/server-info.md
```

Không để thành `/home/container/craftopia-ai-support-bot/bot.py`. Nếu bị bọc thêm thư mục, hãy chuyển toàn bộ file ra `/home/container`.

## 3. Startup

Với egg Python Generic, đặt:

```text
PY_FILE=bot.py
REQUIREMENTS_FILE=requirements.txt
PY_PACKAGES=
USER_UPLOAD=1
AUTO_UPDATE=0
```

Giữ startup command mặc định của egg để Panel tự cài `requirements.txt`. Nếu hosting cho nhập startup command thủ công, có thể dùng:

```bash
python3 -m pip install --user -r requirements.txt && exec python3 -u bot.py
```

`requirements.txt` cài `discord.py[voice] 2.7.1`, `aiohttp 3.14.3`, `yt-dlp 2026.8.19` và `imageio-ffmpeg 0.6.0`. `imageio-ffmpeg` kèm binary FFmpeg dự phòng nên không cài `ffmpeg` vào `PY_PACKAGES`; biến này chỉ dành cho package Python. Bot ưu tiên FFmpeg hệ thống có sẵn trong image Pterodactyl. Dockerfile của dự án cài cả `ffmpeg` và `libopus0`; trên Python Generic không có libopus, bot vẫn có thể dùng chế độ Opus do FFmpeg mã hoá.

Nếu Panel cứ hiện `STARTING` dù bot đã online, nhờ nhà cung cấp đổi startup-done marker thành:

```text
Craftopia bot logged in as
```

## 4. Tạo file `.env`

Tạo `/home/container/.env` trong File Manager. Không đặt token trong startup command, ZIP hoặc ảnh chụp màn hình.

```dotenv
DISCORD_TOKEN=token-bot-discord
DISCORD_GUILD_ID=ID_SERVER_DISCORD_CRAFTOPIA
GEMINI_API_KEY=api-key-google-ai-studio
GEMINI_MODEL=gemini-3.5-flash-lite
SERVER_NAME=Craftopia

# Kênh chuyên để thành viên hỏi AI bằng tin nhắn thường.
AI_AUTO_REPLY_CHANNEL_IDS=ID_KENH_HOI_AI

# Kênh dự phòng nhận cảnh báo nếu STATUS_CHANNEL_ID=0.
SUPPORT_CHANNEL_IDS=ID_KENH_HOI_AI
STAFF_ROLE_ID=ID_ROLE_STAFF
STATUS_CHANNEL_ID=ID_KENH_STATUS

# Để 0 rồi chạy /stats_setup, hoặc điền ID kênh dashboard ngay tại đây.
STATS_CHANNEL_ID=0
STATS_UPDATE_SECONDS=120

# false chạy ngay với số tổng hợp xấp xỉ. Xem mục 5 trước khi đổi thành true.
TRACK_DISCORD_PRESENCE=false

# Craftopia Music. ID role DJ=0 nghĩa là stop/leave chỉ dành cho Manage Server.
MUSIC_ENABLED=true
# false giữ bot trong voice 24/7; true mới bật tự rời khi queue/kênh trống.
MUSIC_AUTO_LEAVE=false
MUSIC_DJ_ROLE_ID=0
MUSIC_MAX_QUEUE=100
MUSIC_MAX_PER_USER=20
MUSIC_MAX_PLAYLIST=20
MUSIC_MAX_DURATION_SECONDS=10800
MUSIC_ALLOW_LIVE=false
MUSIC_IDLE_SECONDS=180
MUSIC_VOICE_DISCONNECT_GRACE_SECONDS=12
MUSIC_RESOLVE_TIMEOUT_SECONDS=35
MUSIC_RESOLVE_WORKERS=2
MUSIC_BITRATE_KBPS=128
MUSIC_DEFAULT_VOLUME=80
MUSIC_ALLOWED_HOSTS=youtube.com,youtu.be,soundcloud.com
# Để trống để tự ưu tiên FFmpeg hệ thống rồi mới dùng binary imageio dự phòng.
FFMPEG_PATH=

MONITOR_ALL_CHANNELS=true
MONITOR_EXCLUDED_CHANNEL_IDS=ID_KENH_STAFF,ID_KENH_TICKET,ID_KENH_LOG

MC_HOST=play.craftopics.online
JAVA_PORT=25565
BEDROCK_PORT=19132
STATUS_CHECK_SECONDS=60
STATUS_CACHE_SECONDS=15
STATUS_FAILURE_THRESHOLD=3
STATUS_RECOVERY_THRESHOLD=2

MONITOR_WINDOW_SECONDS=180
MONITOR_COOLDOWN_SECONDS=900
MONITOR_MAX_ALERTS_PER_HOUR=3

MAX_AI_CONCURRENCY=3
MAX_QUESTIONS_PER_MINUTE=4
MAX_HISTORY_MESSAGES=6
MAX_HISTORY_MINUTES=30
# Tổng dung lượng tối đa của tất cả ảnh trong một yêu cầu AI.
MAX_IMAGE_MB=8
MAX_UPLOAD_MB=25
MAX_TRAINING_CHARS=50000

# Giới hạn an toàn cho chức năng xem trước/xoá tin nhắn hôm nay.
DELETE_SCAN_LIMIT_PER_CHANNEL=5000
DELETE_SCAN_LIMIT_TOTAL=20000
DELETE_MAX_MESSAGES=500
DELETE_CONFIRM_SECONDS=120
DELETE_SCAN_COOLDOWN_SECONDS=30

SYNC_COMMANDS=true
LOG_LEVEL=INFO
```

Nhiều ID phải cách nhau bằng dấu phẩy và không có dấu ngoặc. Bật Developer Mode trong Discord, bấm phải server/kênh/role rồi chọn **Copy ID**.

`AI_AUTO_REPLY_CHANNEL_IDS` chỉ nên chứa kênh chuyên hỏi AI. Tin nhắn văn bản trong kênh đó được gửi tới Gemini. Không điền kênh chat chung, staff, ticket, thanh toán hoặc log.

## 5. Cấu hình Discord

Trong Discord Developer Portal:

1. Vào **Bot** và bật **Message Content Intent**.
2. Giữ `TRACK_DISCORD_PRESENCE=false` nếu chỉ cần số online xấp xỉ. Nếu muốn số chi tiết, bật thêm **Server Members Intent** và **Presence Intent**, sau đó mới đổi biến này thành `true`.
3. Invite bot với scopes `bot` và `applications.commands`.
4. Cấp các quyền:
   - View Channel;
   - Send Messages;
   - Send Messages in Threads;
   - Read Message History;
   - Manage Messages;
   - Embed Links;
   - Attach Files;
   - Use Application Commands;
   - Connect;
   - Speak.

`DISCORD_GUILD_ID` giúp slash command được đồng bộ ngay vào Craftopia. Nếu để `0`, bot đồng bộ global và Discord có thể cần thời gian mới hiển thị lệnh.

`TRACK_DISCORD_PRESENCE=true` yêu cầu đồng thời **Server Members Intent** và **Presence Intent**. Nếu Portal chưa bật mà `.env` đã đặt `true`, Discord có thể ngắt kết nối bot với Gateway close code `4014`. Với bot đã được xác minh và hoạt động trong nhiều server, Discord còn có thể yêu cầu phê duyệt privileged intents.

## 6. Cấu hình Craftopia Music

Panel nhạc mang author **DiskiiVN**; trạng thái bot trên Discord hiển thị **Đang nghe “by DiskiiVN”**.

Để trống `FFMPEG_PATH` là cấu hình khuyến nghị: bot tự chọn theo thứ tự FFmpeg hệ thống trong `PATH`, rồi binary dự phòng của `imageio-ffmpeg`. Chỉ điền đường dẫn tuyệt đối khi cần ép một executable cụ thể. Sau khi restart, vào Voice Channel rồi chạy `/music_diagnose`; kết quả hiển thị dependency, phiên bản, chính xác đường dẫn executable đang dùng, số binary dự phòng và quyền View/Connect/Speak.

Bot chỉ hỗ trợ Voice Channel, chưa hỗ trợ Stage Channel. Người gọi `/play` hoặc `/music` phải ở voice; người phát hoặc điều khiển phải ở cùng voice với bot. Chỉ `/queue` và `/nowplaying` là các lệnh xem có thể dùng ngoài voice. `/play tên bài` tìm trên YouTube; `/play URL` mặc định chỉ nhận YouTube và SoundCloud theo `MUSIC_ALLOWED_HOSTS`. Không cần YouTube API key. Mặc định `MUSIC_AUTO_LEAVE=false` giữ bot trong voice 24/7. Chỉ khi đổi thành `true`, bot mới tự rời sau `MUSIC_IDLE_SECONDS` giây khi không phát gì hoặc voice không còn người nghe. Khi voice Discord chập chờn, bot chờ `MUSIC_VOICE_DISCONNECT_GRACE_SECONDS` giây để discord.py tự nối lại trước khi đóng phiên nhạc.

Khi nhận tên bài hoặc link một bài, bot chạy bắt tay Discord Voice và lượt tìm `yt-dlp` song song, sau đó tái sử dụng URL stream của lượt tìm đầu tiên nên luồng bình thường không phải resolve lần hai ngay trước khi phát. Playlist vẫn được đọc dạng phẳng/lazy và xử lý từng mục khi cần để giữ thời gian chờ và tài nguyên ở mức an toàn. Tốc độ lên nhạc thực tế vẫn phụ thuộc vào mạng outbound của hosting, nguồn nhạc và Discord Voice; không có biến `.env` riêng cho cơ chế này.

Lệnh slash và prefix tương ứng gồm `/play`/`~play`, `/music`/`~music`, `/queue`/`~queue`, `/nowplaying`/`~nowplaying`, `/pause`/`~pause`, `/resume`/`~resume`, `/skip`/`~skip`, `/shuffle`/`~shuffle`, `/loop`/`~loop`, `/volume`/`~volume`, `/stop`/`~stop`, `/leave`/`~leave` và `/music_diagnose`/`~music_diagnose`. Prefix còn có alias `~q`, `~np` và `~s`. `loop` nhận `off`, `track` hoặc `queue`; volume nhận 10–100.

`/play` và `/music` tự mở panel 10 nút: tạm dừng/phát tiếp, skip, stop, loop, shuffle, form thêm bài, queue, giảm/tăng volume và leave. Nếu panel hiện tại ở cùng text channel, bot cập nhật/tái sử dụng tin đó thay vì spam panel mới; chuyển sang text channel khác sẽ vô hiệu hoá panel cũ. `/music` có thể tạo panel trước khi bot kết nối voice: người đang ở voice bấm **Thêm bài**, gửi form, rồi bot tự kết nối và bắt đầu phát. Các nút khác yêu cầu bot đã kết nối và người bấm đang cùng voice. Stop/leave và hai nút tương ứng chỉ dành cho role `MUSIC_DJ_ROLE_ID`, Manage Server hoặc Administrator.

Các giới hạn mặc định: queue 100 bài, 20 bài/người, 20 mục/playlist, tối đa 10.800 giây/bài, livestream tắt, resolve timeout 35 giây với 2 worker, bitrate 128 kbps và volume 80%. Mọi lượt tìm từ lệnh hoặc form Add đều chống gửi trùng: một in-flight/người/server, tối thiểu 3 giây giữa hai lượt của cùng người và tối đa `max(4, MUSIC_RESOLVE_WORKERS × 2)` lượt đồng thời trên server; `/play` còn có cooldown 2 lần/10 giây. Queue/panel chỉ ở RAM và sẽ mất khi restart. Chỉ phát nội dung bạn có quyền sử dụng và tuân thủ điều khoản YouTube/SoundCloud.

## 7. Tạo dashboard server

Có hai cách chọn kênh theo dõi:

- giữ `STATS_CHANNEL_ID=0`, khởi động bot rồi chạy `/stats_setup` trong kênh muốn đặt dashboard;
- hoặc điền sẵn ID kênh vào `STATS_CHANNEL_ID`, bot sẽ tạo dashboard khi chạy.

`/stats_setup` chỉ dành cho người có quyền **Manage Server**. Bot cần View Channel, Send Messages, Read Message History và Embed Links trong kênh đó. Dashboard chỉ dùng một tin nhắn và chỉnh sửa nó sau mỗi `STATS_UPDATE_SECONDS` giây; giá trị tối thiểu là 60 giây.

Với cấu hình mặc định `TRACK_DISCORD_PRESENCE=false`, số thành viên/online có ký hiệu `≈` vì là số tổng hợp xấp xỉ từ Discord. Bot không lưu danh sách thành viên hay trạng thái của từng người; file `data/stats-dashboard.json` chỉ chứa ID server, kênh và tin nhắn để dashboard tiếp tục cập nhật sau restart.

## 8. Cấu hình xoá tin nhắn an toàn

Người có **Manage Messages** và **Read Message History** trong kênh hiện tại có thể mention/reply bot hoặc gửi trong kênh AI chuyên dụng:

```text
bạn hãy xoá tin nhắn của @Diskiivn trong hôm nay
```

Mặc định bot chỉ quét kênh hiện tại. Thêm `trong toàn server` để quét mọi kênh text và public thread đang hoạt động; phạm vi này chỉ dành cho người có **Administrator**. Private thread và thread đã lưu trữ không được quét. Slash command tương đương là `/delete_today`, với `target` là thành viên và `all_server` mặc định `false`.

Bot phải có **View Channel**, **Read Message History** và **Manage Messages** tại từng kênh cần xử lý. Với phạm vi toàn server, bot dừng mà không hiện nút xác nhận nếu có bất kỳ kênh nào không quét được. Chỉ chủ server có thể chuẩn bị xoá tin nhắn của Owner/Administrator; bot tải mới vai trò của mục tiêu từ Discord trước lúc quét và trước khi xác nhận. Nên chỉ cấp Manage Messages tại các kênh Craftopia mà bot thực sự cần quản trị.

Bot quét từ 00:00 đến thời điểm yêu cầu theo **UTC+7**, giữ lại tin ghim, tin hệ thống và chính tin ra lệnh. Kết quả chỉ là bản xem trước; chính người yêu cầu phải bấm **Xác nhận xoá** trong `DELETE_CONFIRM_SECONDS` giây (mặc định 120). Không bấm hoặc bấm Huỷ thì không có tin nào bị xoá. Sau khi xác nhận, thao tác không thể hoàn tác.

Các giới hạn mặc định trong `.env` là 5.000 tin được quét mỗi kênh, 20.000 tin cho toàn yêu cầu, tối đa 500 tin khớp, 120 giây để xác nhận và 30 giây cooldown giữa hai lần quét. Bot từ chối xoá nếu chạm giới hạn khiến bản quét không đầy đủ. Mã chỉ sử dụng metadata cần thiết; nội dung lịch sử không được gửi tới Gemini hoặc lưu trên ổ đĩa, và chỉ message ID trong bản xem trước được giữ tạm trong RAM.

## 9. Network của hosting

Nhà cung cấp phải cho phép kết nối outbound:

- DNS và TCP 443 tới Discord, Gemini, YouTube và SoundCloud;
- outbound UDP tới Discord Voice; cổng đích do Discord cấp động khi bot kết nối voice;
- TCP 25565 tới Java status;
- UDP 19132 tới Bedrock status.

Nếu `/mcstatus` chỉ lỗi Bedrock, hãy hỏi hosting có chặn outbound UDP hay không.

Nếu bot chat bình thường nhưng voice bị timeout hoặc không có tiếng, hãy hỏi hosting có chặn outbound UDP tới Discord Voice hay không. UDP 19132 của Bedrock và UDP voice là hai luồng riêng.

## 10. Khởi động và kiểm tra

Sau khi bấm **Start**, Console phải hiện:

```text
Craftopia bot logged in as ...
```

Kiểm tra theo thứ tự:

1. `/bot_diagnose` — kiểm tra quyền, API key và kênh tự trả lời;
2. `/ai_test` — gửi một request kiểm tra không chứa chat người dùng;
3. `~ai server ip là gì?` — kiểm tra prefix command;
4. gửi `server ip là gì?` trong `AI_AUTO_REPLY_CHANNEL_IDS`;
5. `/mcstatus` — kiểm tra Java và Bedrock.
6. `/serverstats` — kiểm tra số thành viên Discord và trạng thái Minecraft;
7. `/stats_setup` trong kênh dashboard — kiểm tra tạo và cập nhật một tin nhắn;
8. `/delete_today target:@tài-khoản-thử all_server:false` — kiểm tra bản xem trước rồi bấm **Huỷ**;
9. vào Voice Channel và chạy `/music_diagnose` — kiểm tra dependency, FFmpeg, quyền voice và Voice States Intent;
10. `/play nhạc minecraft`, thử queue/skip/volume trên panel rồi dùng `/leave` bằng role DJ hoặc Manage Server;
11. `~hl Test | Đây là kiến thức thử` bằng tài khoản có Manage Server, sau đó hỏi lại AI.

## 11. Dạy AI bằng `~hl`

Người có quyền **Manage Server** có thể dùng:

```text
~hl Nội dung cần học
~hl Chủ đề | Nội dung cần học
```

Bot còn đọc toàn bộ nội dung của tin nhắn do chính người chạy lệnh đã gửi và đang reply, cùng tối đa 3 tệp `.md`/`.txt` UTF-8 đính kèm ở lệnh hoặc tin nhắn đó. Tổng tệp tối đa 1 MB; ảnh và định dạng khác được bỏ qua. Khi reply, gõ `~hl` để dùng chủ đề mặc định hoặc `~hl Tên chủ đề` để tự đặt chủ đề. Có thể kết hợp nội dung lệnh, reply và tệp trong một bài học.

`MAX_TRAINING_CHARS` là giới hạn tổng số ký tự của một bài (mặc định 50.000). Nếu vượt giới hạn, bot từ chối và không cắt âm thầm. Nội dung giống token, API key, mật khẩu, webhook hoặc private key cũng bị từ chối. Bài học được lưu lâu dài trong `knowledge/managed/` và có thể được gửi tới Gemini khi phù hợp với câu hỏi. Không đăng bí mật hoặc dữ liệu cá nhân vào Discord để thử bộ lọc; hãy thay/revoke ngay nếu token đã từng bị lộ.

## 12. Lỗi thường gặp

- `ModuleNotFoundError`: `REQUIREMENTS_FILE` phải là `requirements.txt`, sau đó reinstall/restart server.
- Bot online nhưng không trả lời tin nhắn: kiểm tra `AI_AUTO_REPLY_CHANNEL_IDS`, Message Content Intent và quyền Send Messages/Read Message History.
- Mention hoạt động nhưng tin nhắn thường không hoạt động: ID kênh tự trả lời đang sai hoặc để trống.
- Slash command không xuất hiện: kiểm tra `DISCORD_GUILD_ID`, `SYNC_COMMANDS=true` và scope `applications.commands`.
- `/ai_test` báo 401/403: API key sai hoặc chưa có quyền dùng model.
- `/ai_test` báo 429: free tier đang hết quota, chờ quota khôi phục.
- Bot mất kết nối với code `4014`: đặt `TRACK_DISCORD_PRESENCE=false`, hoặc bật đủ **Server Members Intent** và **Presence Intent** trên Developer Portal rồi restart.
- Dashboard không tạo/cập nhật: kiểm tra `STATS_CHANNEL_ID`, quyền View/Send/Read History/Embed Links và file `data/stats-dashboard.json` có quyền ghi.
- Xoá tin nhắn báo thiếu quyền hoặc không quét được kênh: cấp **View Channel**, **Read Message History** và **Manage Messages** cho bot tại đúng kênh; người chạy lệnh cũng cần Manage Messages/Read History, còn phạm vi toàn server cần Administrator.
- Bản xem trước báo vượt giới hạn: dùng phạm vi kênh hiện tại hoặc tăng `DELETE_SCAN_LIMIT_PER_CHANNEL`, `DELETE_SCAN_LIMIT_TOTAL` hay `DELETE_MAX_MESSAGES` có kiểm soát rồi restart bot; bot cố ý không xoá khi bản quét chưa đầy đủ.
- Lệnh nhạc không xuất hiện: kiểm tra `MUSIC_ENABLED=true`, `SYNC_COMMANDS=true`, restart bot và chờ guild/global slash sync; prefix vẫn cần Message Content Intent.
- `/music_diagnose` báo thiếu package: giữ `REQUIREMENTS_FILE=requirements.txt` rồi reinstall; các dependency cần có là `discord.py[voice]`, `yt-dlp` và `imageio-ffmpeg`.
- FFmpeg thiếu/không chạy: xoá giá trị sai trong `FFMPEG_PATH` để bot tự tìm FFmpeg hệ thống và binary dự phòng, hoặc điền đúng đường dẫn executable do hosting cung cấp.
- Console báo `Unrecognized option reconnect...`: bảo đảm đang dùng `music.py` mới; bản hiện tại chỉ dùng nhóm reconnect nền tương thích với cả FFmpeg cũ.
- Console báo FFmpeg `return code -11`: đó là native crash. Bản hiện tại tránh binary Linux lỗi bằng cách ưu tiên FFmpeg hệ thống, ưu tiên nguồn HTTPS trực tiếp và tự refresh/retry đúng một lần bằng candidate dự phòng. Chạy `/music_diagnose` để xem executable thật đang được chọn.
- Console báo FFmpeg `return code -9` ngay sau `voice handshake is being terminated`: FFmpeg đang bị cleanup do voice/bot/container dừng; đây không phải FFmpeg tự crash.
- Pterodactyl báo `Exit code: 137`: toàn container đã nhận `SIGKILL`; riêng mã này không cho biết user, watchdog, Docker daemon hay kernel/node nào đã gửi tín hiệu. `Out of memory: false` chỉ nghĩa là Wings/Docker không đánh dấu **container OOM**, chưa loại trừ memory pressure/OOM ở toàn node. Kiểm tra **Activity/Audit log**, resource graph, Scheduled Tasks và nhờ hosting đối chiếu Wings/Docker/kernel log đúng thời điểm. Bản bot giới hạn voice cleanup còn 3 giây để tránh mắc kẹt trong shutdown, nhưng không chương trình nào có thể bắt hoặc từ chối `SIGKILL` từ host.
- Bot chỉ rời voice nhưng process vẫn online: giữ `MUSIC_AUTO_LEAVE=false`. Nếu muốn hành vi tự dọn kênh, đặt `true` và chỉnh `MUSIC_IDLE_SECONDS`.
- Bot không vào voice: kiểm tra người gọi đã ở Voice Channel, bot có View/Connect/Speak, bot chưa phục vụ voice khác và hosting cho outbound UDP Discord Voice.
- Bot vào voice nhưng không có tiếng: chạy `/music_diagnose`; thiếu system libopus vẫn có fallback Opus FFmpeg, nhưng hosting phải cho phép FFmpeg chạy và truy cập nguồn qua TCP 443.
- Link bị từ chối hoặc không tìm thấy: chỉ dùng domain trong `MUSIC_ALLOWED_HOSTS`; video riêng tư, giới hạn tuổi/vùng hoặc cần đăng nhập không được hỗ trợ.
- Console không có log ready: xem lỗi ngay phía trên; không liên tục bấm Restart vì có thể tạo crash-loop.

## 13. Backup

Sao lưu định kỳ:

```text
knowledge/managed/
knowledge/imports/
data/stats-dashboard.json
.env
```

Nếu bản cũ còn `knowledge/admin-facts.md`, hãy sao lưu file đó cùng các mục trên. Không chia sẻ file `.env`. Dữ liệu trong `knowledge/` và `data/stats-dashboard.json` phải được giữ lại khi cập nhật bot.

Hàng đợi, bài đang phát và panel Craftopia Music không được lưu trên ổ đĩa; restart/backup không khôi phục phiên nhạc đang chạy.
