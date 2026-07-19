-- 024 (2026-07-19, Timeline del directo): serie temporal de las stats en vivo.
--
-- PROBLEMA: live_fight_stats (017/018) es un SNAPSHOT — PK fight_id, el bucle
-- del directo lo pisa cada ~20 s y la historia se pierde. La "película" del
-- combate (evolución de golpes/derribos/control a lo largo de los asaltos)
-- necesita TODAS las muestras.
--
-- DISEÑO: tabla APARTE append-only, misma filosofía que 016/017 — las guardas
-- se cumplen POR CONSTRUCCIÓN:
--   1. live_fight_stats NO se toca: el snapshot y su semántica delicada
--      ('post' pegajoso, merge por lado, is_final inmutable, skip-list del
--      bucle, poda 48 h) siguen intactos. /en-vivo sigue leyendo de allí.
--   2. El ÚNICO escritor es espn_live_results (insert_live_fight_stat_sample,
--      tras cada upsert del snapshot): copia la fila FUSIONADA con dedup
--      contra la última muestra — el cron de respaldo live-results (*/10)
--      solapado no duplica muestras idénticas. El backfill desde capturas
--      (backfill_live_samples_from_capture) escribe con sampled_at histórico.
--   3. Se filtran los walkouts (period 0, stats a cero) y las muestras sin
--      stats: la serie empieza con el primer asalto.
--   4. `fights`, `fight_stats` y `fight_stats_rounds` no se tocan y el modelo
--      ML no ve estas filas (misma separación que 017).
--   5. SIN poda: la serie se conserva PARA SIEMPRE (decisión del dueño,
--      19-jul) — es el producto ("lo del vídeo no lo tiene nadie"). Volumen
--      ~600-900 filas/evento. Una pelea cancelada borra su serie (mismo
--      criterio que delete_live_fight_stats); borrar la pelea la borra en
--      cascada.
--   6. Lectoras: /fights/[id] (la película post-evento) y /en-vivo (mini
--      timeline en directo) en mma-app.
--
-- stats: JSONB {"<fighter_id>": {kd,tsl,tsa,ssl,ssa,hl,ha,bl,ba,ll,la,tdl,
-- tda,sub,ctrl}} con claves = fighters.id NUESTROS y ctrl en segundos (mismo
-- contrato que live_fight_stats.stats, siempre la foto ACUMULADA del combate).
-- Eje temporal de la UI: (period - 1) * 300 + (300 - reloj) con display_clock.
--
-- Aditiva e idempotente (IF NOT EXISTS); no toca datos existentes.

CREATE TABLE IF NOT EXISTS live_fight_stat_samples (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fight_id INTEGER NOT NULL REFERENCES fights(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN ('in', 'post')),
    -- Estado fino de ESPN, crudo, como en 017 ('STATUS_END_OF_ROUND', 'End R2').
    status_name TEXT,
    status_detail TEXT,
    period INTEGER,
    display_clock TEXT,
    stats JSONB NOT NULL,
    sampled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- La consulta de la web es siempre "la serie de UNA pelea, en orden".
CREATE INDEX IF NOT EXISTS idx_live_fight_stat_samples_fight_sampled
    ON live_fight_stat_samples (fight_id, sampled_at);
