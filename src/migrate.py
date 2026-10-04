def run_migrate(repository):
    """Apply idempotent data upgrades for deployments created before the
    handover feature.

    Result batches without a handover are treated as not taken over: their
    original lot is derived from the linked QC run and preserved. Historical
    QC results and original lot numbers remain queryable.
    """
    return repository.backfill_result_batch_lots()
