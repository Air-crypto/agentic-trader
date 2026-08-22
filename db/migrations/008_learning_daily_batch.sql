CREATE UNIQUE INDEX IF NOT EXISTS learning_prediction_batches_one_per_date
    ON learning_prediction_batches (decision_date);
