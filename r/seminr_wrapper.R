#!/usr/bin/env Rscript
# seminr_wrapper.R
# Usage: Rscript seminr_wrapper.R <csv_path> <json_output_path> <model_spec_json>
# model_spec_json: { measurement: { ConstructName: [items...], ... }, structural: { Dep: [Indep...], ... } }

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 3) {
  write(jsonlite::toJSON(list(error = "Expected 3 args: csv_path, json_output_path, model_spec_json"), auto_unbox = TRUE), stdout())
  quit(status = 1)
}

csv_path <- args[1]
json_path <- args[2]
model_spec_json <- args[3]

suppressPackageStartupMessages({
  if (!requireNamespace("seminr", quietly = TRUE)) install.packages("seminr", repos = "https://cloud.r-project.org")
  if (!requireNamespace("jsonlite", quietly = TRUE)) install.packages("jsonlite", repos = "https://cloud.r-project.org")
})

library(seminr)
library(jsonlite)

to_to_json <- function(x) {
  if (is.data.frame(x)) return(as.list(as.data.frame(t(x))))
  if (is.list(x)) return(lapply(x, to_to_json))
  if (is.matrix(x)) return(apply(x, c(1,2), as.numeric))
  if (is.null(x)) return(NA_real_)
  return(x)
}

out <- list(
  measurement_loadings = list(),
  reliability = list(),
  paths = list(),
  r_squared = list(),
  vif = list(),
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
    items_vec <- spec$measurement[[nm]]
    if (length(items_vec) == 0 || any(is.na(items_vec))) stop(sprintf("Empty items for construct %s", nm))
    construct_list[[nm]] <- items(items_vec)
  }
  measurement_model <- do.call(constructs, construct_list)
  
  # Build structural paths
  path_list <- list()
  for (dep in names(spec$structural)) {
    indeps <- spec$structural[[dep]]
    for (indep in indeps) {
      path_list <- c(path_list, as.formula(paste0("`", dep, "` ~ `", indep, "`")))
    }
  }
  if (length(path_list) == 0) stop("structural model has no paths")
  structural_model <- do.call(paths, path_list)
  
  model <- pls_model(
    measurement_model,
    structural_model
  )
  
  results <- pls(data, model)
  boot_results <- boot(results, R = 200)
  summary_res <- summary(boot_results)
  
  # Loadings
  loadings_df <- summary_res$loadings
  out$measurement_loadings <- split(round(as.numeric(loadings_df$loading), 4), rownames(loadings_df))
  
  # Reliability
  rel_df <- summary_res$reliability
  for (i in seq_len(nrow(rel_df))) {
    rn <- rownames(rel_df)[i]
    out$reliability[[rn]] <- list(
      cronbach_alpha = round(as.numeric(rel_df$cronbach_alpha[i]), 4),
      rho_a = round(as.numeric(rel_df$rho_a[i]), 4),
      composite_reliability = round(as.numeric(rel_df$composite_reliability[i]), 4),
      ave = round(as.numeric(rel_df$ave[i]), 4)
    )
  }
  
  # Paths
  path_df <- summary_res$path_coefs
  for (i in seq_len(nrow(path_df))) {
    out$paths[[sprintf("%s -> %s", path_df$independent[i], path_df$dependent[i])]] <- list(
      beta = round(as.numeric(path_df$path_coef[i]), 4),
      std_err = round(as.numeric(path_df$std_error[i]), 4),
      t_stat = round(as.numeric(path_df$t_stat[i]), 4),
      p_value = round(as.numeric(path_df$p_value[i]), 6)
    )
  }
  
  # R-squared / VIF
  if (!is.null(summary_res$r_squared)) {
    for (nm in names(summary_res$r_squared)) {
      out$r_squared[[nm]] <- round(as.numeric(summary_res$r_squared[[nm]]), 4)
    }
  }
  if (!is.null(summary_res$vif)) {
    vif_df <- summary_res$vif
    for (i in seq_len(nrow(vif_df))) {
      out$vif[[rownames(vif_df)[i]]] <- round(as.numeric(vif_df$vif[i]), 4)
    }
  }
  
}, error = function(e) {
  out$error <- paste("seminr failed:", e$message)
})

write(toJSON(out, auto_unbox = TRUE, na = "null"), json_path)
quit(status = ifelse(is.null(out$error), 0L, 1L))
