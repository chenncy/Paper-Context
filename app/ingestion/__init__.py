"""论文 Ingestion Pipeline：parse → clean → chunk → enrich → index → validate。"""
from app.ingestion.pipeline import IngestionResult, run_ingestion_pipeline

__all__ = ["IngestionResult", "run_ingestion_pipeline"]
