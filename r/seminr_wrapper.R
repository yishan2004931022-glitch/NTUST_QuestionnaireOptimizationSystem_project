#!/usr/bin/env Rscript
# seminr_wrapper.R
# Usage: Rscript seminr_wrapper.R <csv_path> <json_output_path> <model_spec_json> <boot_iterations>
# model_spec_json: { measurement: { ConstructName: [items...], ... }, structural: { Dep: [Indep...], ... } }
#
# NOTE on API: this targets seminr >= 2.5.0. estimate_pls()/bootstrap_model() replaced the
# older pls()/pls_model()/boot() names, composite() takes a plain item vector (no items()
# wrapper), and paths() must be wrapped in relationships() to produce a valid structural
# model object -- calling paths() alone returns a flat character vector, not a matrix, and
# will make estimate_pls() fail with "incorrect number of dimensions".
#
# SRMR is intentionally NOT computed here: seminr has no built-in SRMR function, and no
# independently-validated formula was available at the time this was written. Do not add
# an ad-hoc SRMR calculation without cross-checking the result against SmartPLS or another
# reference implementation first -- a silently wrong fit statistic is worse than a missing one.

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 3) {
  write(jsonlite::toJSON(list(error = "Expected at least 3 args: csv_path, json_output_path, model_spec_json, [boot_iterations]"), auto_unbox = TRUE), stdout())
  quit(status = 1)
}

csv_path <- args[1]
json_path <- args[2]
model_spec_json <- args[3]
boot_iterations <- if (length(args) >= 4) as.integer(args[4]) else 200L

suppressPackageStartupMessages({
  if (!requireNamespace("seminr", quietly = TRUE)) install.packages("seminr", repos = "https://cloud.r-project.org")
  if (!requireNamespace("jsonlite", quietly = TRUE)) install.packages("jsonlite", repos = "https://cloud.r-project.org")
})

library(seminr)
library(jsonlite)

matrix_to_nested_list <- function(m) {
  # Square validity/effect-size matrices (htmt, fSquare) -> {row: {col: value}}, dropping NA/"." cells.
  if (is.null(m)) return(list())
  out <- list()
  rn <- rownames(m)
  cn <- colnames(m)
  for (i in seq_along(rn)) {
    row_out <- list()
    for (j in seq_along(cn)) {
      v <- suppressWarnings(as.numeric(m[i, j]))
      if (!is.na(v)) row_out[[cn[j]]] <- round(v, 4)
    }
    if (length(row_out) > 0) out[[rn[i]]] <- row_out
  }
  out
}

out <- list(
  measurement_loadings = list(),
  reliability = list(),
  validity = list(htmt = list()),
  f_squared = list(),
  paths = list(),
  r_squared = list(),
  vif = list(),
  predictive = list(),
  composite_scores = list(),
  error = NULL
)

tryCatch({
  data <- read.csv(csv_path, check.names = FALSE, stringsAsFactors = FALSE)

  spec <- fromJSON(model_spec_json, simplifyVector = FALSE)
  if (!is.list(spec) || is.null(spec$measurement) || is.null(spec$structural)) {
    stop("model_spec_json must have 'measurement' and 'structural' lists")
  }

  # Build measurement model
  construct_names <- names(spec$measurement)
  construct_list <- list()
  for (nm in construct_names) {
    items_vec <- unlist(spec$measurement[[nm]])
    if (length(items_vec) == 0 || any(is.na(items_vec))) stop(sprintf("Empty items for construct %s", nm))
    construct_list[[nm]] <- composite(nm, items_vec)
  }
  measurement_model <- do.call(constructs, construct_list)

  # Build structural paths: relationships() wraps one or more paths(from=, to=) calls.
  path_calls <- list()
  for (dep in names(spec$structural)) {
    indeps <- unlist(spec$structural[[dep]])
    if (length(indeps) == 0) next
    path_calls[[length(path_calls) + 1]] <- paths(from = indeps, to = dep)
  }
  if (length(path_calls) == 0) stop("structural model has no paths")
  structural_model <- do.call(relationships, path_calls)

  results <- estimate_pls(data = data, measurement_model = measurement_model, structural_model = structural_model)
  boot_results <- bootstrap_model(results, nboot = boot_iterations, seed = 123)
  s <- summary(results)
  boot_s <- summary(boot_results)

  # Real PLS-weighted composite scores (one value per respondent per
  # construct) -- these are the actual iterative-algorithm outer weights,
  # not an approximation. Used by /analyze/composite when a structural
  # model is supplied.
  cs <- results$construct_scores
  if (!is.null(cs)) {
    for (nm in colnames(cs)) {
      out$composite_scores[[nm]] <- round(as.numeric(cs[, nm]), 4)
    }
  }

  # Loadings
  loadings_df <- s$loadings
  out$measurement_loadings <- split(round(as.numeric(loadings_df), 4), rownames(loadings_df))

  # Reliability
  rel_df <- s$reliability
  for (i in seq_len(nrow(rel_df))) {
    rn <- rownames(rel_df)[i]
    out$reliability[[rn]] <- list(
      cronbach_alpha = round(as.numeric(rel_df[i, "alpha"]), 4),
      rho_a = round(as.numeric(rel_df[i, "rhoA"]), 4),
      composite_reliability = round(as.numeric(rel_df[i, "rhoC"]), 4),
      ave = round(as.numeric(rel_df[i, "AVE"]), 4)
    )
  }

  # HTMT (discriminant validity) -- s$validity$htmt is a lower-triangular matrix
  out$validity$htmt <- matrix_to_nested_list(s$validity$htmt)

  # f-squared effect sizes -- rows = predictor, cols = outcome
  out$f_squared <- matrix_to_nested_list(s$fSquare)

  # Bootstrapped path coefficients + significance
  path_df <- boot_s$bootstrapped_paths
  for (i in seq_len(nrow(path_df))) {
    rn <- rownames(path_df)[i]  # e.g. "TRU  -> BI"
    out$paths[[trimws(rn)]] <- list(
      beta = round(as.numeric(path_df[i, "Original Est."]), 4),
      boot_mean = round(as.numeric(path_df[i, "Bootstrap Mean"]), 4),
      std_err = round(as.numeric(path_df[i, "Bootstrap SD"]), 4),
      t_stat = round(as.numeric(path_df[i, "T Stat."]), 4),
      ci_2_5 = round(as.numeric(path_df[i, "2.5% CI"]), 4),
      ci_97_5 = round(as.numeric(path_df[i, "97.5% CI"]), 4),
      p_value = round(as.numeric(path_df[i, "Bootstrap P Val"]), 6)
    )
  }

  # R-squared -- s$paths has "R^2"/"AdjR^2" rows, one column per dependent construct
  if (!is.null(s$paths) && "R^2" %in% rownames(s$paths)) {
    r2_row <- s$paths["R^2", , drop = FALSE]
    adj_row <- if ("AdjR^2" %in% rownames(s$paths)) s$paths["AdjR^2", , drop = FALSE] else NULL
    for (nm in colnames(r2_row)) {
      v <- suppressWarnings(as.numeric(r2_row[1, nm]))
      if (!is.na(v)) {
        entry <- list(r_squared = round(v, 4))
        if (!is.null(adj_row)) {
          av <- suppressWarnings(as.numeric(adj_row[1, nm]))
          if (!is.na(av)) entry$adj_r_squared <- round(av, 4)
        }
        out$r_squared[[nm]] <- entry
      }
    }
  }
  if (!is.null(s$vif_antecedents)) {
    for (dep in names(s$vif_antecedents)) {
      vif_vec <- s$vif_antecedents[[dep]]
      out$vif[[dep]] <- as.list(round(as.numeric(vif_vec), 4))
      names(out$vif[[dep]]) <- names(vif_vec)
    }
  }

  # Q2predict / PLSpredict (Shmueli et al. 2019 out-of-sample procedure).
  # Uses k-fold CV (not LOOCV) to keep runtime bounded on larger uploads.
  pred <- tryCatch(
    predict_pls(results, technique = predict_DA, noFolds = 10),
    error = function(e) NULL
  )
  if (!is.null(pred) && !is.null(pred$items)) {
    # Only endogenous (dependent) constructs' indicators get out-of-sample
    # predictions -- exogenous constructs have nothing upstream to predict them.
    items <- colnames(pred$items$PLS_out_of_sample_residuals)
    for (it in items) {
      actual <- pred$items$item_actuals[, it]
      pls_resid <- pred$items$PLS_out_of_sample_residuals[, it]
      lm_resid <- pred$items$lm_out_of_sample_residuals[, it]
      sse <- sum(pls_resid^2, na.rm = TRUE)
      sso <- sum((actual - mean(actual, na.rm = TRUE))^2, na.rm = TRUE)
      q2predict <- if (sso > 0) 1 - sse / sso else NA_real_
      rmse_pls <- sqrt(mean(pls_resid^2, na.rm = TRUE))
      rmse_lm <- sqrt(mean(lm_resid^2, na.rm = TRUE))
      out$predictive[[it]] <- list(
        q2predict = if (is.na(q2predict)) NULL else round(q2predict, 4),
        rmse_pls = round(rmse_pls, 4),
        rmse_lm_benchmark = round(rmse_lm, 4),
        beats_lm_benchmark = rmse_pls < rmse_lm
      )
    }
  }

}, error = function(e) {
  out$error <<- paste("seminr failed:", e$message)
})

write(toJSON(out, auto_unbox = TRUE, na = "null"), json_path)
quit(status = ifelse(is.null(out$error), 0L, 1L))
