# Hype Radar

Bir kateqoriya (məs. "GTA 6") əlavə edirsən. Motor onu Twitch, Kick və YouTube-da tapır,
canlı yayımları və son 24 saatın anlarını hype-a görə sıralayır və dashboard-a canlı ötürür.
API açarı lazım deyil.

```bash
./run.sh            # http://localhost:8090
.venv/bin/python -m pytest -q
```

## Necə işləyir
- `engine/connectors/`: hər platforma üçün bir fayl. Açarsız, saytların öz public endpoint-lərindən istifadə edir.
  Rəsmi olmadıqları üçün dəyişə bilərlər. Platforma qırmızı olarsa, səbəb dashboard-da nöqtənin üstündə görünür.
- `engine/scoring/hype.py`: hype = izləyici sayı (0.25) + 10 dəqiqəlik artım (0.30) + chat sürəti (0.25) + klip aktivliyi (0.20).
  Hamısı kateqoriya daxilində ranklanır. Olmayan siqnal neytral (0.5) sayılır. Çəkilər Ayarlarda dəyişir.
- `engine/chat/`: Twitch IRC və Kick Pusher, anonim qoşulur. Hər platformada top 15 kanal izlənir.
  Chat partlayışı (z ≥ 3) "Anlar" bölməsinə VOD linki ilə düşür.
- Keyfiyyət: Twitch və Kick üçün HLS master playlist-dən (1080p60 və s.).
- Əməllər: linki kopyala, OBS-ə göndər (obs-websocket v5, "HypeRadar" Browser Source), klip hazırla (chopify).
- Klip: platforma klipi → klipin özü; chat partlayışı → VOD-dan yalnız ±40 san (yt-dlp); canlı yayım → `chopify --live`.
  Nəticələr "Hazır kliplər" tab-ında: MP4 yüklə + yayımçı kreditli paylaşım təsviri.

## Quraşdırılmış pulsuz alətlər
- chopify: `/home/k4/projects/chopify` (Python 3.12 venv, branch `fix/cli-none-defaults`, `av<16`)
- OBS Studio: `flatpak run com.obsproject.Studio` (WebSocket :4455, parol Ayarlarda)

## Klaviatura
`↑ ↓` hərəkət · `C` link · `O` OBS · `K` klip · `/` axtarış · `Esc` bağla

## Qeyd
Başqasının yayımını react yayımında göstərmək və ya kliplərini paylaşmaq yayımçının icazəsini tələb edə bilər.
Hər yayımçı üçün icazə statusu var. "Yoxdur" seçiləndə klip əməli bağlanır.
