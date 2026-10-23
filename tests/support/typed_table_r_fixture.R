args <- commandArgs(TRUE)
.libPaths(c(args[[1]], .libPaths()))
library(scworkbench)
bundle <- wb_init_bundle(args[[2]])
values <- c(.0499545077, 0.049999999999999996, .05,
  0.05000000000000001, 1e-50, 1e-300, 0, 1 / -Inf,
  pi, .Machine$double.xmax, .Machine$double.xmin, 5e-324,
  NA_real_, NaN, Inf, -Inf)
stopifnot(identical(1 / values[[8]], -Inf))
table <- data.frame(feature_id = paste0("f", seq_along(values)),
  value = values, count = rep(c(NA_integer_, 1L), 8),
  flag = rep(c(TRUE, FALSE, NA, TRUE), 4),
  label = rep(c("NA", "NaN", NA_character_, "雪:-0,}"), 4),
  missing_double = rep(NA_real_, 16), missing_integer = rep(NA_integer_, 16),
  missing_flag = rep(NA, 16), missing_label = rep(NA_character_, 16))
publish <- function(prim) {
  run <- wb_new_run("metadata_cell_vector", outputs = list(vector = prim))
  result <- wb_promote_analysis(bundle, setNames(list(run), run$run_id),
    run$run_id, outputs = "vector")
  result$promoted$artifact_id[[1]]
}
ids <- list(
  typed = publish(wb_prim_obs_table(table)),
  features = publish(wb_prim_feature_table(table)),
  empty = publish(wb_prim_obs_table(table[0, ])),
  projection = publish(wb_prim_obs_table(data.frame(
    group = factor(c("a", "b")), day = as.Date(c("2020-01-01", "2020-01-02"))))))
legacy <- wb_prim_obs_table(data.frame(value = .0499545077), meta = list(
  storage_format = "scworkbench.table.typed-json.v1",
  columns = list(names = "value", types = "double"), meta = list(label = "legacy")))
legacy$table_storage <- NULL
ids$legacy <- publish(legacy)
stopifnot(wb_finalize_recipient_bundle(bundle)$ready)
wb_close_bundle(bundle)
jsonlite::write_json(ids, args[[3]], auto_unbox = TRUE)
