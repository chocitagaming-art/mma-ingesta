-- 017 (2026-07-11, T3-A fase B): stats EN VIVO por pelea desde ESPN.
--
-- DISEÑO: tabla APARTE (misma filosofía que 016) — las guardas se cumplen
-- POR CONSTRUCCIÓN:
--   1. `fights`, `fight_stats` y `fight_stats_rounds` NO se tocan: el flujo
--      oficial del lunes (backfill_results desde ufcstats) sigue siendo la
--      única fuente de esas tablas. Aquí no hay conflicto COALESCE posible
--      entre provisional y oficial porque viven en tablas distintas.
--   2. El modelo ML (features/db.py lee `fights`) no ve estas filas.
--   3. El ÚNICO escritor es espn_live_results (bucle de ventana de evento +
--      cron de respaldo): UPSERT completo por pasada — es dato VIVO, la
--      última lectura gana (a diferencia de fights, donde el almacenado gana).
--   4. La ÚNICA lectora es la página /en-vivo de mma-app (tarjeta EN CURSO
--      con stats en directo y totales provisionales al terminar cada pelea,
--      que hoy tardan hasta el lunes en llegar por ufcstats).
--
-- Verificado con muestras reales de UFC 329 (11-jul-2026, capturador T3-A):
-- ESPN publica las estadísticas EN DIRECTO durante el asalto vía
-- sports.core.api.espn.com .../competitions/<id>/competitors/<athleteId>/statistics
-- y el estado fino por pelea (Walkouts / In Progress / End of Round, asalto,
-- reloj) en el scoreboard. timeInControl llega en SEGUNDOS.
--
-- stats: JSONB {"<fighter_id>": {kd,tsl,tsa,ssl,ssa,tdl,tda,sub,ctrl}} con
-- claves = fighters.id NUESTROS (resueltos por el matcher de live-results);
-- ctrl en segundos. NULL si ESPN aún no publicó nada para la pelea.
--
-- state: 'in' mientras la pelea está en curso; 'post' cuando el bucle guardó
-- los totales finales provisionales (y deja de re-pedirla).
--
-- Las filas caducan solas: el escritor poda updated_at > 48 h (la web solo
-- consulta las peleas del evento del día).
--
-- Aditiva e idempotente (IF NOT EXISTS); no toca datos existentes.

CREATE TABLE IF NOT EXISTS live_fight_stats (
    fight_id INTEGER PRIMARY KEY REFERENCES fights(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN ('in', 'post')),
    -- Estado fino de ESPN, crudo: name ('STATUS_END_OF_ROUND'...) y detail
    -- ('End R2'...). El frontend traduce los conocidos y degrada al detail.
    status_name TEXT,
    status_detail TEXT,
    period INTEGER,
    display_clock TEXT,
    stats JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
