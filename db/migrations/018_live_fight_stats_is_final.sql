-- 018 (2026-07-12, T3-A fase B — arreglo de la revisión adversarial): sellado
-- SEGURO del total final en live_fight_stats.
--
-- Contexto (hallazgos 1/2 de docs/MMA_REVISION_T3A_FASEB_2026-07-12.json):
-- get_final_stats_fight_ids marcaba una pelea como "final" en cuanto veía
-- state='post' AND stats IS NOT NULL. Como durante la pelea cada pasada
-- escribe stats, al pasar a 'post' la columna casi nunca es NULL: si el fetch
-- de stats fallaba justo en la pasada de cierre, se congelaba un total de
-- MITAD de pelea (o de un solo lado) como "final provisional" para siempre —
-- la guarda de reintento documentada era código muerto.
--
-- FIX: columna is_final. El escritor solo la pone a TRUE cuando, ya en 'post',
-- tiene stats FRESCAS de cada lado que ESPN puede servir en ESA pasada. La
-- skip-list del bucle pasa a mirar is_final (no state/stats), así que un cierre
-- con fetch fallido se REINTENTA barato hasta capturar el total completo.
-- is_final es MONOTÓNICA (el UPSERT hace OR con el valor guardado): una vez
-- sellado un final bueno, un escritor solapado con datos peores no lo desella.
--
-- Aditiva e idempotente (ADD COLUMN IF NOT EXISTS); no toca datos existentes.
-- Las filas ya escritas quedan is_final=FALSE por defecto: si el evento sigue
-- vivo se re-piden y sellan bien; si ya pasó, la poda de 48 h las retira.

ALTER TABLE live_fight_stats
    ADD COLUMN IF NOT EXISTS is_final BOOLEAN NOT NULL DEFAULT FALSE;
