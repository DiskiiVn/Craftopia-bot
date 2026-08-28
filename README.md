# Craftopia AI Support Bot

Bot Discord hỗ trợ người chơi Minecraft bằng Gemini API free tier và kho kiến thức riêng của Craftopia. Bot vẫn giữ công cụ chuyển pack Oraxen sang Geyser.

## Tính năng

- `/ask`: hỏi AI bằng văn bản và tối đa một ảnh lỗi;
- hỏi bằng `/ask`, `~ai câu hỏi`, mention/reply bot hoặc nhắn trong kênh AI chuyên dụng;
- chỉ lấy thông tin riêng của Craftopia từ thư mục `knowledge/`;
- ghi nguồn tài liệu nội bộ bên dưới câu trả lời;
- nhớ ngữ cảnh ngắn, `/ai_reset` để xóa;
- nút gọi role staff khi AI không đủ thông tin;
- `/ai_reload` dành cho người có quyền Manage Server;
- `/ai_teach` để quản trị viên dạy AI một thông tin mới ngay trong Discord;
- `~hl` học toàn bộ nội dung admin nhập, tin nhắn đang reply và tối đa 3 tệp `.md`/`.txt` UTF-8;
- `/ai_import` để nhập luật, lệnh hoặc FAQ dạng `.md`/`.txt`;
- `/ai_test` kiểm tra API key/model/quota thật và `/bot_diagnose` kiểm tra quyền Discord;
- `/ip` hiển thị địa chỉ Java và Bedrock/PE mà không tốn lượt AI;
- `/mcstatus` kiểm tra trực tiếp trạng thái Java và Bedrock/PE, gồm số người chơi và độ trễ khi server phản hồi;
- `/serverstats` xem tổng thành viên, số đang online trên Discord và trạng thái Minecraft;
- `/stats_setup` tạo một tin nhắn dashboard tự cập nhật trong kênh do admin chọn;
- Craftopia Music phát YouTube/SoundCloud bằng slash hoặc prefix command, có hàng đợi và bảng điều khiển 10 nút;
- hiểu câu quản trị như `xoá tin nhắn của @user trong hôm nay` và có `/delete_today`, luôn xem trước rồi mới cho xác nhận xoá;
- giám sát cục bộ các tin nhắn mới để nhận biết nhiều người cùng báo lag, mất kết nối hoặc xung đột;
- tự kiểm tra trạng thái Minecraft định kỳ và báo vào kênh trạng thái sau nhiều lần lỗi liên tiếp;
- chống spam, giới hạn đồng thời và vô hiệu hóa mention do AI tạo;
- `/convert`: chuyển ZIP gộp Oraxen/resource pack sang Geyser.

## 1. Tạo API key Gemini miễn phí

1. Truy cập [Google AI Studio](https://aistudio.google.com/apikey).
2. Đăng nhập Google và chọn **Create API key**.
3. Không đăng API key lên Discord hoặc GitHub. Chỉ điền nó vào file `.env` trên máy chạy bot.

Free tier không yêu cầu nạp credit nhưng có giới hạn lượt sử dụng. Theo chính sách free tier của Google, nội dung gửi tới API có thể được dùng để cải thiện sản phẩm; không đưa mật khẩu, token, mã 2FA hoặc dữ liệu thanh toán vào bot.

Lưu ý điều khoản Gemini API hiện hành yêu cầu người dùng API từ 18 tuổi và không cho dùng API trong ứng dụng hướng tới hoặc có khả năng được người dưới 18 tuổi truy cập. Nếu Discord Craftopia có thành viên dưới 18 tuổi, không bật phần trả lời Gemini công khai; các chức năng `/ip`, `/mcstatus`, monitor cục bộ và converter vẫn hoạt động không cần Gemini. Xem [Gemini API Additional Terms](https://ai.google.dev/gemini-api/terms).

## 2. Tạo Discord bot

1. Vào [Discord Developer Portal](https://discord.com/developers/applications), tạo Application và Bot.
2. Trong trang **Bot**, bật **Message Content Intent**.
3. Nếu muốn số online chi tiết với `TRACK_DISCORD_PRESENCE=true`, bật thêm **Server Members Intent** và **Presence Intent**. Có thể bỏ qua bước này khi giữ cấu hình mặc định `false`.
4. Tạo invite với scopes `bot`, `applications.commands`.
5. Cấp các quyền: View Channels, Send Messages, Send Messages in Threads, Read Message History, **Manage Messages**, Embed Links, Attach Files, Use Application Commands, **Connect** và **Speak**.

## 3. Cài và chạy

Yêu cầu Python 3.11–3.14. Voice dùng `discord.py[voice] 2.7.1`; nguồn nhạc dùng `yt-dlp 2026.8.19`; `imageio-ffmpeg 0.6.0` cung cấp binary FFmpeg dự phòng nên không cần cài gói `ffmpeg` bằng pip. Bot ưu tiên FFmpeg hệ thống nếu hosting đã có. Gói vẫn dùng PyYAML 6.0.3 để có wheel dựng sẵn cho Python 3.14 trên Windows. Bot tự đọc `.env` và gọi Gemini bằng thư viện chuẩn, nên không cần cài `python-dotenv` hoặc `httpx`.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Mở `.env` và điền:

```dotenv
DISCORD_TOKEN=token-discord
DISCORD_GUILD_ID=ID-server-discord
GEMINI_API_KEY=api-key-google-ai-studio
SERVER_NAME=Craftopia
SUPPORT_CHANNEL_IDS=
AI_AUTO_REPLY_CHANNEL_IDS=
STAFF_ROLE_ID=0
STATUS_CHANNEL_ID=0
STATS_CHANNEL_ID=0
STATS_UPDATE_SECONDS=120
TRACK_DISCORD_PRESENCE=false
MUSIC_ENABLED=true
MUSIC_AUTO_LEAVE=false
MUSIC_DJ_ROLE_ID=0
MUSIC_MAX_QUEUE=100
MUSIC_MAX_PER_USER=20
MUSIC_MAX_PLAYLIST=20
MUSIC_MAX_DURATION_SECONDS=10800
MUSIC_ALLOW_LIVE=false
MUSIC_IDLE_SECONDS=180
MUSIC_VOICE_DISCONNECT_GRACE_SECONDS=30
MUSIC_VOICE_RECONNECT_ATTEMPTS=4
MUSIC_VOICE_RECONNECT_BACKOFF_SECONDS=2
MUSIC_PLAYBACK_RETRIES=4
MUSIC_PLAYBACK_RETRY_BACKOFF_SECONDS=2
MUSIC_STREAM_RW_TIMEOUT_SECONDS=45
MUSIC_RESOLVE_TIMEOUT_SECONDS=35
MUSIC_RESOLVE_WORKERS=2
MUSIC_BITRATE_KBPS=128
MUSIC_DEFAULT_VOLUME=80
MUSIC_ALLOWED_HOSTS=youtube.com,youtu.be,soundcloud.com
FFMPEG_PATH=
DELETE_SCAN_LIMIT_PER_CHANNEL=5000
DELETE_SCAN_LIMIT_TOTAL=20000
DELETE_MAX_MESSAGES=500
DELETE_CONFIRM_SECONDS=120
DELETE_SCAN_COOLDOWN_SECONDS=30
MONITOR_ALL_CHANNELS=true
MONITOR_EXCLUDED_CHANNEL_IDS=
MC_HOST=play.craftopics.online
JAVA_PORT=25565
BEDROCK_PORT=19132
MAX_TRAINING_CHARS=50000
```

`AI_AUTO_REPLY_CHANNEL_IDS` là danh sách kênh chuyên hỏi AI; bot tự trả lời mọi tin nhắn văn bản trong các kênh này. `SUPPORT_CHANNEL_IDS` chỉ làm kênh dự phòng nhận cảnh báo nếu `STATUS_CHANNEL_ID=0`. Để tương thích bản cũ, nếu chưa có biến `AI_AUTO_REPLY_CHANNEL_IDS`, bot dùng `SUPPORT_CHANNEL_IDS`; nếu biến tồn tại nhưng để trống thì auto-reply bị tắt.

Trước khi bật `MONITOR_ALL_CHANNELS=true`, hãy điền ID của kênh staff, ticket riêng, log, kênh nhạy cảm và các kênh không muốn theo dõi vào `MONITOR_EXCLUDED_CHANNEL_IDS`. Các ID cách nhau bằng dấu phẩy. `STATUS_CHANNEL_ID` là nơi nhận cảnh báo Java/Bedrock; nếu để `0`, bot dùng kênh support đầu tiên khi có thể.

Chạy bot:

```powershell
python bot.py
```

Hoặc chạy bằng Docker:

```bash
docker compose up -d --build
```

Dockerfile đã cài `ffmpeg` và `libopus0` để dùng binary của Debian, PCM và đổi âm lượng ngay trong bài. Nếu máy/hosting không có system libopus, bot tự dùng Opus do FFmpeg mã hoá; nhạc vẫn phát nhưng thay đổi âm lượng có thể chỉ áp dụng từ bài kế tiếp. Thứ tự chọn executable là `FFMPEG_PATH` hợp lệ, FFmpeg trong `PATH`, rồi mới đến binary dự phòng của `imageio-ffmpeg`. Để trống `FFMPEG_PATH` là cấu hình khuyến nghị.

Để chạy 24/7 trên hosting, dùng gói Pterodactyl và làm theo [PTERODACTYL.md](PTERODACTYL.md).

Đưa source mới lên GitHub **không tự cập nhật** các file đang chạy trên Pterodactyl. Panel chỉ lấy bản mới khi thư mục `/home/container` là Git repository và startup thực sự chạy `git pull` với `AUTO_UPDATE=1`; nếu đang dùng `AUTO_UPDATE=0`, upload ZIP hoặc thư mục không có `.git`, hãy dừng server, thay các file bằng bản mới rồi khởi động lại. Kiểm tra timestamp/nội dung `music.py` trên Panel để chắc chắn hosting không còn chạy bản cũ.

## 4. Dạy bot thông tin Craftopia

Sửa hoặc thêm file `.md`/`.txt` trong `knowledge/`, ví dụ:

- `server-info.md`: IP Java/Bedrock, port, phiên bản;
- `rules.md`: luật và hình phạt;
- `commands.md`: lệnh, rank và quyền;
- `faq.md`: lỗi thường gặp;
- `store.md`: chính sách cửa hàng, không lưu thông tin thanh toán bí mật.

Sau khi sửa tài liệu, quản trị viên dùng `/ai_reload`. Hãy điền địa chỉ Java/Bedrock thật vào `knowledge/server-info.md` trước khi đưa bot vào hoạt động.

Craftopia hiện được cấu hình với Java `play.craftopics.online` và Bedrock/PE `play.craftopics.online:19132`. Để bổ sung kiến thức mà không sửa file, dùng `/ai_teach`; để đưa cả tài liệu vào bot, dùng `/ai_import`.

Ví dụ huấn luyện nhanh:

```text
~hl Lệnh survival | Người chơi dùng /warp survival để đến khu sinh tồn.
~hl Giờ bảo trì | Server bảo trì lúc 04:00 sáng Chủ nhật khi có thông báo từ staff.
~hl Người chơi được nhận kit tân thủ một lần bằng /kit newbie.
```

`~hl` cũng học được từ tin nhắn đang reply và tệp đính kèm:

1. Reply một tin nhắn do chính bạn đã gửi rồi gõ `~hl` để dùng chủ đề mặc định.
2. Reply rồi gõ `~hl Luật PvP` để đặt chủ đề cho nội dung được reply.
3. Đính kèm tệp rồi gõ `~hl Chủ đề tài liệu`; bot nhận tối đa 3 tệp `.md`/`.txt` UTF-8, tính cả tệp trong tin nhắn đang reply, với tổng dung lượng tối đa 1 MB.
4. Có thể kết hợp `~hl Chủ đề | nội dung`, một tin nhắn reply và các tệp trong cùng lần dạy; bot lưu đủ từng phần làm một bài học.

Mỗi bài học tối đa `MAX_TRAINING_CHARS` ký tự (mặc định 50.000, có thể đặt từ 3.000 đến 200.000). Nếu vượt giới hạn, bot từ chối và báo số ký tự thay vì âm thầm cắt mất kiến thức. Bot cũng từ chối nội dung có dạng Discord token, API key, mật khẩu, webhook hoặc private key. `~hl` chỉ nhận reply do chính người chạy lệnh đã gửi; ảnh và định dạng tệp khác được bỏ qua. Nội dung được lưu lâu dài trên ổ đĩa và có thể được đưa vào yêu cầu Gemini khi phù hợp, vì vậy không dùng `~hl` để lưu bí mật, thông tin thanh toán hoặc dữ liệu cá nhân không cần thiết.

Kiến thức do `~hl` tạo nằm trong `knowledge/managed/`; một bài giống hệt bài đã có sẽ không bị ghi trùng. Chỉ thành viên có quyền **Manage Server** mới dùng được `~hl`, `/ai_teach`, `/ai_import`, `/ai_reload` và `/stats_setup`.

## 5. Theo dõi Discord và Minecraft

`/serverstats` tạo bảng thống kê tức thời gồm tổng thành viên Discord, số đang online và trạng thái Java/Bedrock. `/stats_setup` dành cho người có quyền **Manage Server**: chạy lệnh trong kênh muốn đặt dashboard, bot sẽ tạo một tin nhắn duy nhất rồi sửa tin nhắn đó sau mỗi `STATS_UPDATE_SECONDS` giây (tối thiểu 60 giây). Có thể pin tin nhắn thủ công.

Để tự tạo dashboard khi bot khởi động, đặt `STATS_CHANNEL_ID` thành ID kênh. Nếu để `0`, chạy `/stats_setup`; bot nhớ kênh và message ID trong `data/stats-dashboard.json` qua các lần restart.

Mặc định `TRACK_DISCORD_PRESENCE=false`. Bot dùng số tổng hợp xấp xỉ do Discord cung cấp, chạy được mà không cần hai privileged intents về thành viên/presence; ký hiệu `≈` trên dashboard cho biết số xấp xỉ. Muốn đếm chi tiết từ member cache, hãy bật **Server Members Intent** và **Presence Intent** trong Developer Portal trước, sau đó mới đặt `TRACK_DISCORD_PRESENCE=true` và restart bot. Nếu bật biến nhưng chưa bật cả hai Intent trên Portal, Discord có thể ngắt bot với Gateway close code `4014`.

Dashboard chỉ lưu ID server/kênh/tin nhắn để cập nhật đúng vị trí. Bot không lưu danh sách tên, ID hoặc trạng thái online của từng thành viên; chỉ hiển thị số tổng hợp.

## 6. Craftopia Music

Panel nhạc mang author **DiskiiVN**; trạng thái bot trên Discord hiển thị **Đang nghe “by DiskiiVN”**.

Người dùng phải vào một **Voice Channel** trước khi dùng `/play`, `/music` hoặc bắt đầu phát từ panel; Stage Channel chưa được hỗ trợ. Bot cần View Channel, Connect và Speak tại voice đó. Một server chỉ có một phiên nhạc. Sau khi bot kết nối, mọi thao tác phát/điều khiển hoặc bấm nút đều yêu cầu người dùng đang ở cùng voice; ngoại lệ khởi tạo là nút **Thêm bài** trên panel mới, khi đó bot sẽ kết nối vào voice của chính người gửi form. Chỉ các lệnh xem `/queue` và `/nowplaying` có thể dùng ngoài voice.

Mọi lệnh nhạc là hybrid command: tên slash và prefix `~` có cùng chức năng. Các alias ngắn chỉ áp dụng cho prefix:

| Slash command | Prefix command | Chức năng |
|---|---|---|
| `/play query` | `~play query` | Tìm trên YouTube hoặc thêm URL YouTube/SoundCloud vào hàng đợi; tự kết nối voice và gửi panel |
| `/music` | `~music` | Mở/cập nhật bảng điều khiển 10 nút; người gọi phải ở voice |
| `/queue` | `~queue`, `~q` | Xem hàng đợi |
| `/nowplaying` | `~nowplaying`, `~np` | Xem bài đang phát |
| `/pause` | `~pause` | Tạm dừng |
| `/resume` | `~resume` | Tiếp tục phát |
| `/skip` | `~skip`, `~s` | Bỏ qua bài hiện tại |
| `/shuffle` | `~shuffle` | Trộn hàng đợi |
| `/loop [mode]` | `~loop [off\|track\|queue]` | Tắt lặp, lặp một bài hoặc lặp hàng đợi; bỏ trống để chuyển chế độ kế tiếp |
| `/volume percent` | `~volume 10-100` | Đặt âm lượng từ 10% đến 100% |
| `/stop` | `~stop` | Dừng và xoá hàng đợi; cần role DJ hoặc Manage Server |
| `/leave` | `~leave` | Rời voice và kết thúc phiên; cần role DJ hoặc Manage Server |
| `/music_diagnose` | `~music_diagnose` | Kiểm tra dependency voice, DAVE, FFmpeg, audio mode và quyền; cần Manage Server |

`/play` và `/music` tự mở panel gồm 10 nút: tạm dừng/phát tiếp, skip, stop, chuyển loop, shuffle, mở form thêm bài, xem queue, giảm/tăng âm lượng 10% và leave. Nếu panel hiện tại đã nằm trong cùng text channel, bot sửa và tái sử dụng đúng tin đó thay vì gửi thêm; khi chuyển sang text channel khác, panel cũ bị vô hiệu hoá và panel hiện tại được gửi ở kênh mới. `/music` chỉ chuẩn bị panel và yêu cầu người gọi đang ở voice; khi bot chưa kết nối, nút **Thêm bài** vẫn mở form, sau đó bot kết nối voice của người gửi và bắt đầu phát. Các nút khác chỉ hoạt động khi bot đã ở voice. Nút stop/leave vẫn cần role có ID trong `MUSIC_DJ_ROLE_ID` hoặc quyền **Manage Server**; Administrator cũng được chấp nhận. Đặt `MUSIC_DJ_ROLE_ID=0` để chỉ dùng quyền Manage Server/Administrator.

Nhập tên bài sẽ tìm một kết quả YouTube. URL chỉ được nhận nếu hostname nằm trong `MUSIC_ALLOWED_HOSTS`; mặc định là `youtube.com`, `youtu.be` và `soundcloud.com`. Link dùng IP trực tiếp, credential trong URL, cổng lạ hoặc domain ngoài allowlist bị từ chối. Bot không cần YouTube API key. Chỉ phát nội dung bạn có quyền sử dụng và tuân thủ điều khoản của nguồn.

Để bài đầu lên nhanh nhất có thể, bot chạy bắt tay kết nối Discord Voice song song với lượt tìm `yt-dlp`. Với tên bài hoặc link một bài, URL stream lấy được ở lượt tìm đầu tiên được tái sử dụng khi phát nên luồng bình thường không phải resolve lần hai ngay trước khi có tiếng. Playlist vẫn được đọc dạng phẳng/lazy và chỉ xử lý từng mục khi cần để tránh tải toàn bộ playlist. Thời gian thực tế vẫn phụ thuộc vào mạng của hosting, tốc độ nguồn nhạc và Discord Voice.

Mọi yêu cầu tìm nhạc từ `/play`, `~play` hoặc form **Thêm bài** đều qua cùng lớp chống spam: mỗi người chỉ có một yêu cầu đang xử lý trong mỗi server, hai yêu cầu của cùng người cách nhau ít nhất 3 giây và số lượt tìm đồng thời của server được giới hạn ở `max(4, MUSIC_RESOLVE_WORKERS × 2)`. Riêng `/play`/`~play` còn có cooldown tối đa 2 lần trong 10 giây cho mỗi thành viên. Hãy chờ yêu cầu hiện tại hoàn tất thay vì gửi lặp lại.

Các giới hạn mặc định gồm 100 bài trong queue, 20 bài giữ chỗ cho mỗi người, tối đa 20 mục mỗi playlist, thời lượng 10.800 giây/bài, không nhận livestream, timeout resolve 35 giây, 2 worker, bitrate 128 kbps và âm lượng 80%. `MUSIC_AUTO_LEAVE=false` giữ bot trong voice 24/7; đổi thành `true` mới bật tự rời sau `MUSIC_IDLE_SECONDS` giây không hoạt động/không còn người nghe. Khi voice Discord rớt, bot chờ `MUSIC_VOICE_DISCONNECT_GRACE_SECONDS=30`, sau đó thử nối lại tối đa `MUSIC_VOICE_RECONNECT_ATTEMPTS=4` lần, cách nhau theo backoff cơ sở `MUSIC_VOICE_RECONNECT_BACKOFF_SECONDS=2`. Khi FFmpeg hoặc URL stream rớt giữa bài, bot refresh nguồn và thử phát tiếp tối đa `MUSIC_PLAYBACK_RETRIES=4` lần với backoff cơ sở `MUSIC_PLAYBACK_RETRY_BACKOFF_SECONDS=2`; `MUSIC_STREAM_RW_TIMEOUT_SECONDS=45` là timeout đọc mạng của FFmpeg. Các biến còn lại tương ứng với `MUSIC_MAX_QUEUE`, `MUSIC_MAX_PER_USER`, `MUSIC_MAX_PLAYLIST`, `MUSIC_MAX_DURATION_SECONDS`, `MUSIC_ALLOW_LIVE`, `MUSIC_RESOLVE_TIMEOUT_SECONDS`, `MUSIC_RESOLVE_WORKERS`, `MUSIC_BITRATE_KBPS` và `MUSIC_DEFAULT_VOLUME`. Đổi `.env` cần restart bot.

Queue, bài đang phát và panel hiện tại chỉ nằm trong RAM; restart bot sẽ kết thúc phiên nhạc. Chạy `/music_diagnose` sau khi triển khai để kiểm tra trước khi mở tính năng cho thành viên. Có thể tắt toàn bộ command nhạc bằng `MUSIC_ENABLED=false` rồi restart.

Nếu Pterodactyl báo `Exit code: 137`, toàn bộ tiến trình đã nhận `SIGKILL` từ host/container. Python không thể bắt hoặc tự phục hồi bên trong tiến trình từ tín hiệu này; hãy kiểm tra resource graph, Activity/Audit log, Scheduled Tasks và log Wings/Docker/kernel của nhà cung cấp. Cơ chế retry nhạc chỉ xử lý lỗi stream, FFmpeg hoặc Discord Voice khi tiến trình bot vẫn còn chạy.

## 7. Xoá tin nhắn hôm nay có xác nhận

Trong kênh AI tự trả lời, có thể gửi trực tiếp:

```text
bạn hãy xoá tin nhắn của @Diskiivn trong hôm nay
bạn hãy xoá tin nhắn của @Diskiivn trong hôm nay trong toàn server
```

Ở kênh khác, hãy mention/reply bot với câu trên hoặc dùng `~ai`. Phải mention đúng một thành viên mục tiêu. Câu không ghi phạm vi chỉ quét **kênh hiện tại**; cụm `trong toàn server` quét các kênh text và public thread đang hoạt động, nhưng chỉ người có quyền **Administrator** mới được dùng phạm vi này. Private thread và thread đã lưu trữ không thuộc phạm vi toàn server.

Có thể dùng slash command tương đương:

```text
/delete_today target:@Diskiivn all_server:false
```

Người chạy lệnh phải có **Manage Messages** và **Read Message History** trong kênh hiện tại; bot cần **View Channel**, **Read Message History** và **Manage Messages** tại những kênh cần quét/xoá. Với phạm vi toàn server, chỉ cần một kênh không quét được là bot dừng và không đưa nút xác nhận, tránh xoá một phần. Chỉ chủ server được chuẩn bị xoá tin nhắn của chính Owner hoặc một thành viên có quyền Administrator; quyền của mục tiêu được tải mới từ Discord cả trước lúc quét lẫn khi xác nhận.

Bot tính “hôm nay” từ 00:00 đến thời điểm quét theo múi giờ **UTC+7**. Tin ghim, tin hệ thống và chính tin dùng để ra lệnh được giữ lại. Bot chưa xoá gì ở bước quét: nó hiện số tin khớp và nút **Xác nhận xoá/Huỷ**; mặc định chỉ chính người tạo yêu cầu có thể xác nhận trong 120 giây. Khi xác nhận, bot chỉ xoá đúng các message ID có trong bản xem trước.

Quét và nhận dạng câu lệnh chạy cục bộ: nội dung lịch sử không được gửi tới Gemini, không được phân tích để huấn luyện và không được lưu vào tệp. Chỉ các message ID cần cho bản xem trước được giữ tạm trong RAM đến khi xác nhận hoặc hết hạn. Thao tác đã xác nhận là xoá vĩnh viễn và **không thể hoàn tác**.

Các giới hạn an toàn có thể chỉnh trong `.env`:

- `DELETE_SCAN_LIMIT_PER_CHANNEL`: số tin tối đa được quét trong mỗi kênh cho hôm nay, mặc định 5.000; bot chấp nhận từ 100 đến 20.000 và dừng, không xoá nếu kết quả bị cắt;
- `DELETE_SCAN_LIMIT_TOTAL`: số tin tối đa được quét trong toàn bộ yêu cầu, mặc định 20.000 và tối đa 100.000; giá trị thực tế không thấp hơn giới hạn mỗi kênh;
- `DELETE_MAX_MESSAGES`: số tin tối đa được phép đưa vào một bản xem trước, mặc định 500, tối đa 1.000; vượt giới hạn thì không xoá;
- `DELETE_CONFIRM_SECONDS`: thời gian chờ xác nhận, mặc định 120 giây, cho phép 30–300 giây;
- `DELETE_SCAN_COOLDOWN_SECONDS`: thời gian chờ giữa hai lần quét của cùng người, mặc định 30 giây, tối thiểu 10 giây.

## 8. Giám sát cục bộ và quyền riêng tư

Bộ giám sát chỉ quan sát **tin nhắn mới** mà bot nhận được sau khi khởi động. Nó không gọi `channel.history()`, không quét lại lịch sử Discord, không tải nội dung tệp đính kèm hoặc liên kết, và không tạo kho lưu trữ toàn bộ cuộc trò chuyện.

Việc nhận biết dấu hiệu bất ổn hoặc xung đột chạy cục bộ bằng từ khóa, cửa sổ thời gian, số tin nhắn và số người báo độc lập. Nội dung được quan sát thụ động bởi monitor **không được gửi tới Gemini** và không tự động ghi vào thư mục `knowledge/`. Gemini chỉ nhận nội dung khi thành viên dùng `/ask`, `~ai`, mention/reply bot hoặc gửi tin nhắn trong `AI_AUTO_REPLY_CHANNEL_IDS` đã được quản trị viên dành riêng cho AI.

Thiết kế này tránh gửi toàn bộ hội thoại sang dịch vụ AI hoặc dùng lịch sử chat làm dữ liệu huấn luyện. Discord cấm khai thác/quét dữ liệu và cấm dùng message content để huấn luyện mô hình AI nếu chưa có sự cho phép rõ ràng của Discord; xem [Discord Developer Policy](https://support-dev.discord.com/hc/en-us/articles/8563934450327-Discord-Developer-Policy).

Monitor bỏ qua bot/webhook, không tag `@everyone`, có cooldown theo kênh để tránh spam và chỉ đưa ra lời nhắc trung lập; mọi quyết định quản trị vẫn thuộc về staff. Khi một endpoint Minecraft không phản hồi, bot chỉ cảnh báo sau số lần thất bại liên tiếp trong `STATUS_FAILURE_THRESHOLD`; một lần timeout đơn lẻ không được coi là server đã sập.

Lịch sử của các câu hỏi được người dùng chủ động gửi cho AI chỉ giữ tối đa số lượt trong `MAX_HISTORY_MESSAGES` và tự xóa khỏi RAM sau `MAX_HISTORY_MINUTES` phút không hoạt động (mặc định 30 phút).

Các biến cấu hình liên quan:

- `MONITOR_ALL_CHANNELS`: bật/tắt theo dõi tin nhắn mới trong các kênh bot nhìn thấy;
- `MONITOR_EXCLUDED_CHANNEL_IDS`: danh sách kênh phải bỏ qua, phân cách bằng dấu phẩy;
- `STATUS_CHANNEL_ID`: kênh nhận cảnh báo trạng thái;
- `MC_HOST`, `JAVA_PORT`, `BEDROCK_PORT`: endpoint cố định được `/mcstatus` và monitor kiểm tra;
- `STATUS_CHECK_SECONDS`: chu kỳ kiểm tra, tối thiểu 30 giây;
- `STATUS_CACHE_SECONDS`: thời gian dùng lại kết quả cho nhiều lệnh `/mcstatus` đồng thời, mặc định 15 giây;
- `STATUS_FAILURE_THRESHOLD`: số lần thất bại liên tiếp trước khi cảnh báo, tối thiểu 2;
- `STATUS_RECOVERY_THRESHOLD`: số lần thành công liên tiếp trước khi báo phục hồi, tối thiểu 2;
- `MONITOR_WINDOW_SECONDS`: khoảng thời gian gom nhiều báo cáo, mặc định 180 giây;
- `MONITOR_COOLDOWN_SECONDS`: thời gian chờ trước khi cùng một kênh báo lại, mặc định 900 giây;
- `MONITOR_MAX_ALERTS_PER_HOUR`: giới hạn cảnh báo chủ động cho mỗi server Discord, mặc định 3/giờ.

`/mcstatus` chỉ truy vấn giao thức status công khai của Minecraft. Lệnh này không truy cập console, log, plugin, tài khoản người chơi hoặc dữ liệu quản trị server.

## ZIP Oraxen cho `/convert`

```text
input.zip
├── resource-pack/
│   ├── pack.mcmeta
│   └── assets/
└── Oraxen/
    ├── items/
    ├── pack/
    ├── glyphs/
    └── settings.yml
```

Không cho `Oraxen.jar` vào ZIP. Converter hiện đảm bảo tốt nhất cho item 2D, Material và display name; model 3D, furniture, armor và shader vẫn phải kiểm tra thủ công trên Bedrock.
