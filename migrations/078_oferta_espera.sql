-- Qué horario guardado se le ofreció a cada persona de la lista de espera y
-- cómo terminó: esperando (guardado vigente) / reservo / vencio (pasó al
-- siguiente) / otro (pidió otro y no había). Lo muestra el panel.
alter table lista_espera add column ofrecido_turno text;
alter table lista_espera add column ofrecido_hasta timestamptz;
alter table lista_espera add column ofrecido_estado text;
