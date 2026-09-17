-- ChirpStack are nevoie de aceste extensii PostgreSQL INAINTE de migratiile
-- lui de schema, altfel migratiile esueaza SILENTIOS (fara nicio eroare in
-- log), nu se creeaza niciun tabel, si login-ul admin nu merge niciodata
-- ("relation user does not exist"). Ruleaza automat la prima initializare a
-- containerului chirpstack-postgres (Postgres executa orice *.sql din
-- /docker-entrypoint-initdb.d/ o singura data, cand volumul de date e gol).
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS hstore;
