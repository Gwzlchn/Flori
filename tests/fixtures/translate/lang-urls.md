# 本周阅读清单

这周整理了几篇关于检索增强生成的资料,原文链接都放在下面,方便以后回看。

第一篇是官方的入门教程,讲得比较系统,适合从头读一遍:https://python.langchain.com/docs/tutorials/retrieval_augmented_generation_with_vector_stores/ 里面的示例代码可以直接运行。

第二篇是一篇综述论文,梳理了检索、重排和生成三个环节的常见做法:https://arxiv.org/abs/2312.10997 还有作者维护的仓库 https://github.com/Tongji-KGLLM/RAG-Survey/blob/main/README.md 持续更新。

第三篇是一篇工程实践的博客,作者分享了在生产环境里踩过的坑,比如切块太大导致召回不准、向量库版本升级以后索引失效等问题:https://www.anyscale.com/blog/a-comprehensive-guide-for-building-rag-based-llm-applications-part-1 读完以后对评估方法也有了更清楚的认识。

最后是两个常用的向量数据库文档 https://qdrant.tech/documentation/concepts/collections/ 和 https://milvus.io/docs/manage-collections.md 以后选型的时候可以对照着看。

备用链接:https://www.together.ai/blog/rag-tutorial-langchain-and-mongodb-atlas-vector-search https://cookbook.openai.com/examples/question_answering_using_embeddings https://huggingface.co/docs/transformers/main/en/model_doc/rag https://www.pinecone.io/learn/series/rag/rerankers/ https://docs.llamaindex.ai/en/stable/understanding/loading/loading/ https://blog.langchain.dev/semi-structured-multi-modal-rag/
