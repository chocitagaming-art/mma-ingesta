-- Careo (Fighter Face-offs): el vídeo oficial de UFC en YouTube por evento.
--
-- La UFC publica la víspera "UFC <Ciudad>: Fighter Face-offs" (Fight Nights) o
-- "UFC <N>: ... Face-offs" (PPV numerados) en su canal oficial. Guardamos solo
-- el video id (11 chars); el front deriva thumbnail + iframe (como fights.video_url).
-- Lo puebla match_faceoffs.py casando el RSS del canal con el evento (ciudad o
-- token "UFC <N>" + "face-off" + ventana de fechas). NULL = aún sin careo.
ALTER TABLE events ADD COLUMN IF NOT EXISTS faceoff_video_id TEXT;
