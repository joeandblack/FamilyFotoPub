CREATE TABLE IF NOT EXISTS native_matching_policies (
 model_id text PRIMARY KEY REFERENCES people_models(id),
 policy jsonb NOT NULL, evidence jsonb NOT NULL, activated_at timestamptz NOT NULL DEFAULT now()
);
