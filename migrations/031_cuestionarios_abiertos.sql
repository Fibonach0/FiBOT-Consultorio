-- Entrevista inicial y cuestionario docente por link: preguntas abiertas.
-- «para» es una referencia opcional que ve quien responde (ej.: «N. A., 3º B»),
-- porque el docente necesita saber de qué alumno se trata.
alter table cuestionarios add column para text;
