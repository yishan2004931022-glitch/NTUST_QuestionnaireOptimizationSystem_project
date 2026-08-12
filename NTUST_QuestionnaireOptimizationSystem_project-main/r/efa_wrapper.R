#!/usr/bin/env Rscript
# efa_wrapper.R
# Usage: Rscript efa_wrapper.R <csv_path> <json_output_path>
# Reads a CSV of items (header row required), runs parallel analysis + EFA,
# and writes a JSON result to <json_output_path>.

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 2) {
  write(jsonlite::toJSON(list(error = "Expected 2 args: <csv_path> <json_output_path>"), auto_unbox = TRUE), stdout())
  quit(status = 1)
}

csv_path <- args[1]
json_path <- args[2]

# Optional: max factors override
max_factors <- 10
if (length(args) >= 3 && !is.na(suppressWarnings(as.integer(args[3]))) ) {
  max_factors <- as.integer(args[3])
}

suppressPackageStartupMessages({
  if (!requireNamespace("psych", quietly = TRUE)) install.packages("psych", repos = "https://cloud.r-project.org")
  if (!requireNamespace("jsonlite", quietly = TRUE)) install.packages("jsonlite", repos = "https://cloud.r-project.org")
})

library(psych)
library(magrittr)
library(jsonlite)

cols_to_use <- NULL
# Try to detect exclusion columns commonly sent from web apps.
ignore_prefixes <- c("id", "timestamp", "name", "email", "created", "completed")

tryCatch({
  raw <- read.csv(csv_path, check.names = FALSE, stringsAsFactors = FALSE)
}, error = function(e) {
  out <- list(error = paste("CSV read failed:", e$message))
  write(toJSON(out, auto_unbox = TRUE), json_path)
  quit(status = 1)
})

# Select numeric columns that look like items (exclude obvious metadata)
numeric_cols <- vapply(raw, function(x) is.numeric(x) || is.integer(x), logical(1))
candidate_names <- names(raw)[numeric_cols]
lower_names <- tolower(candidate_names)
keep <- !vapply(lower_names, function(n) {
  any(grepl(paste0("^", ignore_prefixes, collapse = "|"), n))
}, logical(1))
items <- candidate_names[keep]
if (length(items) == 0) items <- names(raw)[numeric_cols]
if (length(items) == 0) {
  out <- list(error = "No numeric item columns found in CSV.")
  write(toJSON(out, auto_unbox = TRUE), json_path)
  quit(status = 1)
}

# Parallel Analysis using fa.parallel
n_items <- length(items)
max_factors <- min(max(n_items, 2), max_factors, na.rm = TRUE)
max_iter <- min(max(500000, nrow(raw)), 500000)
df_items <- raw[, items, drop = FALSE]

par_suggest <- NA_integer_
cov_method <- "cov"
tryCatch({
  pa <- fa.parallel(df_items, nfactors = max_factors, n.obs = nrow(df_items), fa = "fa", sim = FALSE)
  par_suggest <- as.integer(pa$nfact[1])
  if (!is.finite(par_suggest) || is.na(par_suggest) || par_suggest < 1) par_suggest <- 1L
  if (par_suggest > n_items) par_suggest <- as.integer(n_items)
}, error = function(e) {
  par_suggest <- NA_integer_
})

# EFA with oblimin rotation; if PCA-like stability needed, switch to "minres"/"ml"
efa_factors <- if (is.finite(par_suggest) && !is.na(par_suggest)) par_suggest else min(3L, n_items)
efa_factors <- min(max(efa_factors, 1L), n_items)

efa_fit <- NULL
tryCatch({
  efa_fit <- fa(df_items, nfactors = efa_factors, rotate = "oblimin", fm = "minres", scores = "none")
}, error = function(e) {
  tryCatch({
    efa_fit <- fa(df_items, nfactors = efa_factors, rotate = "varimax", fm = "minres", scores = "none")
  }, error = function(ee) {
    efa_fit <<- NULL
  })
})

# Fallback to PCA if FA fails
if (is.null(efa_fit)) {
  tryCatch({
    efa_fit <- principal(df_items, nfactors = efa_factors, rotate = "varimax")
  }, error = function(ee) {
    efa_fit <<- NULL
  })
}

if (is.null(efa_fit)) {
  out <- list(error = "EFA/PCA failed on this dataset.", items = items, par_suggest = ifelse(is.finite(par_suggest) && !is.na(par_suggest), par_suggest, NA_real_))
  write(toJSON(out, auto_unbox = TRUE), json_path)
  quit(status = 1)
}

# Extract loadings
loadings <- as.matrix(efa_fit$loadings)

rmse_val <- tryCatch({
  v <- efa_fit$RMSEP
  if (is.null(v) || !is.numeric(v)) NA_real_ else as.numeric(round(v, 4))
}, error = function(e) NA_real_)

# Map item -> factor assignments by highest absolute loading
item_assignments <- list()
for (item in items) {
  if (!(item %in% rownames(loadings))) {
    item_assignments[[item]] <- list(factor = NA_integer_, loading = NA_real_)
    next
  }
  vals <- loadings[item, , drop = TRUE]
  if (length(vals) == 1) {
    best <- vals[1]
    if (abs(best) < 0.3) load_sign <- "weak" else load_sign <- ifelse(best >= 0, "positive", "negative")
    item_assignments[[item]] <- list(factor = 1L, loading = as.numeric(round(best, 4)), sign = load_sign)
  } else {
    idx <- which.max(abs(vals))
    best <- vals[idx]
    if (abs(best) < 0.3) load_sign <- "weak" else load_sign <- ifelse(best >= 0, "positive", "negative")
    item_assignments[[item]] <- list(factor = as.integer(idx), loading = as.numeric(round(best, 4)), sign = load_sign)
  }
}

result <- list(
  items = items,
  n = as.integer(nrow(df_items)),
  par_suggest = ifelse(is.finite(par_suggest) && !is.na(par_suggest), as.integer(par_suggest), NA_integer_),
  efa_factors = as.integer(efa_factors),
  rmse = rmse_val,
  # Extra summary fields
  loadings = lapply(rownames(loadings), function(rn) {
    setNames(as.list(round(loadings[rn, ], 4)), colnames(loadings))
  }) %>% setNames(rownames(loadings)),
  item_assignments = item_assignments
)

write(toJSON(result, auto_unbox = TRUE, na = "null"), json_path)
quit(status = 0)
