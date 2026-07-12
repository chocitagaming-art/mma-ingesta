-- 019 (2026-07-12, F1 Tanda 4 — espejo DEFINITIVO de fotos): dos columnas
-- direccionales para la foto "standing" del cara a cara.
--
-- Contexto: la foto standing de ufc.com
-- (event_fight_card_upper_body_of_standing_athlete) mira a un lado segun la
-- ESQUINA en la que el luchador combatio en ESE evento. Verificado empiricamente
-- sobre 40 peleas: esquina ROJA -> token _L_ (33/40), esquina AZUL -> token _R_
-- (39/40). Como en el layout la roja va a la IZQUIERDA y mira a la DERECHA, el
-- token _L_ = foto que mira a la DERECHA. Guardabamos UNA sola standing_body_url
-- por luchador, asi que en un cara a cara NUEVO un luchador colocado en la esquina
-- contraria a la de su ultima foto miraba al lado equivocado (los dos al mismo
-- lado). El parametro itok de la URL esta firmado por ruta, asi que NO se puede
-- derivar la variante espejada cambiando _L_<->_R_ en la URL (da 403, verificado).
-- Solucion definitiva: guardar la variante izquierda y la derecha por separado;
-- el frontend elige la de su esquina (logos SIEMPRE correctos cuando tenemos la
-- direccion real; espejo CSS solo como ultimo recurso para la direccion que falte).
--
--   standing_body_url_l = foto que mira a la DERECHA (token _L_ / esquina roja)
--   standing_body_url_r = foto que mira a la IZQUIERDA (token _R_ / esquina azul)
--
-- Aditiva e idempotente. El seed rellena cada columna desde la standing_body_url
-- ya guardada segun su token (cobertura inmediata sin re-scrapear); el scraper
-- rellena la direccion que falte con el tiempo (first-writer-wins por direccion).

ALTER TABLE fighters
    ADD COLUMN IF NOT EXISTS standing_body_url_l TEXT,
    ADD COLUMN IF NOT EXISTS standing_body_url_r TEXT;

-- Seed idempotente desde el token de la columna existente (solo rellena huecos).
UPDATE fighters
SET standing_body_url_l = standing_body_url
WHERE standing_body_url_l IS NULL
  AND standing_body_url ~ '_L_[0-9]';

UPDATE fighters
SET standing_body_url_r = standing_body_url
WHERE standing_body_url_r IS NULL
  AND standing_body_url ~ '_R_[0-9]';
