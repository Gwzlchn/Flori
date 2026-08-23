import { computed, onMounted, ref } from "vue";

import { apiClient, apiError, type components } from "../api/client";

export function useKnowledgeNavigation() {
  const domains = ref<components["schemas"]["DomainView"][]>([]);
  const collections = ref<components["schemas"]["CollectionView"][]>([]);
  const sources = ref<components["schemas"]["SourceView"][]>([]);
  const selectedDomainId = ref("");
  const selectedCollectionId = ref("");
  const status = ref("正在读取知识库…");

  const selectedDomain = computed(() => domains.value.find((item) => item.domain_id === selectedDomainId.value));
  const selectedCollection = computed(() => collections.value.find((item) => item.collection_id === selectedCollectionId.value));

  function select(domainId: string, collectionId = ""): void {
    selectedDomainId.value = domainId;
    selectedCollectionId.value = collectionId;
  }

  function selectSource(source: components["schemas"]["SourceView"]): void {
    select(source.domain_id, source.collection_ids[0] ?? "");
  }

  async function refresh(): Promise<void> {
    status.value = "正在读取知识库…";
    try {
      const [domainResult, collectionResult, sourceResult] = await Promise.all([
        apiClient.GET("/api/v1/domains"),
        apiClient.GET("/api/v1/collections"),
        apiClient.GET("/api/v1/sources"),
      ]);
      if (!domainResult.data || !collectionResult.data || !sourceResult.data) {
        status.value = apiError(
          domainResult.error ?? collectionResult.error ?? sourceResult.error,
          "library_read_failed: 无法读取知识库。",
        );
        return;
      }
      domains.value = domainResult.data;
      collections.value = collectionResult.data;
      sources.value = sourceResult.data;
      if (!selectedDomainId.value && domains.value[0]) select(domains.value[0].domain_id);
      status.value = `${domains.value.length} 个领域 · ${sources.value.length} 项内容`;
    } catch {
      status.value = "network_temporary: 无法连接知识库。";
    }
  }

  onMounted(() => void refresh());

  return {
    domains, collections, sources, selectedDomainId, selectedCollectionId, selectedDomain,
    selectedCollection, status, select, selectSource, refresh,
  };
}
