import os
from app.config import API_KEY
from dashscope import TextReRank

from langchain_core.tools import tool



# ====================== 模块 4: 重排序 (Rerank) ======================
def rerank_documents(query, docs, top_n=3):
    """统一 rerank 入口：按 RERANK_PROVIDER 分发（dashscope | local | off），失败降级为截断。"""
    if not docs: return []

    from app.config import RERANK_PROVIDER

    provider = (RERANK_PROVIDER or "dashscope").strip().lower()
    if provider == "off":
        return docs[:top_n]
    if provider == "local":
        try:
            from app.rerank_local import rerank_local

            return rerank_local(query, docs, top_n)
        except Exception as e:
            print(f"[WARN] 本地 Rerank 失败（降级截断）: {e}")
            return docs[:top_n]
    return _rerank_dashscope(query, docs, top_n)


def _rerank_dashscope(query, docs, top_n):
    """调用阿里 GTE-Rerank"""
    doc_texts = [doc.page_content for doc in docs]
    try:
        response = TextReRank.call(
            model='gte-rerank',
            query=query,
            documents=doc_texts,
            top_n=top_n,
            api_key=API_KEY
        )

        reranked_docs = []
        if response.status_code == 200:
            for item in response.output.results:
                target_doc = docs[item.index]
                # 保存置信度分数
                target_doc.metadata['relevance_score'] = item.relevance_score
                reranked_docs.append(target_doc)
            return reranked_docs
        else:
            return docs[:top_n]
    except Exception as e:
        print(f"[WARN] Rerank 失败: {e}")
        return docs[:top_n]


# ====================== 工具定义与主流程 ======================
@tool
def search_knowledge_base(query: str):
    """
    当用户询问具体事实、数据、公司信息时调用。
    走统一检索入口（app/rag_service，默认纯向量召回、模式可配置），
    返回带出处（标题/section/页码）的证据片段。
    """
    from app.rag_service import rag_search

    print(f"\n⚙️ [系统触发] 知识库检索工具，查询词：{query}")
    result = rag_search(query)
    if result.get("error"):
        return result["error"]
    if not result.get("hits"):
        return "未找到相关文档。"
    return result["context"]
