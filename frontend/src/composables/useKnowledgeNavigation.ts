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

  function remember(domainId: string, collectionId: string, library: boolean): void {
    const url = new URL(window.location.href);
    if (library) url.searchParams.set("view", "library");
    url.searchParams.set("domain_id", domainId);
    if (collectionId) url.searchParams.set("collection_id", collectionId);
    else url.searchParams.delete("collection_id");
    history.replaceState(null, "", url);
  }

  function select(domainId: string, collectionId = "", persist = true): void {
    selectedDomainId.value = domainId;
    selectedCollectionId.value = collectionId;
    if (persist) remember(domainId, collectionId, true);
  }

  function selectSource(source: components["schemas"]["SourceView"]): void {
    const collectionId = source.collection_ids[0] ?? "";
    select(source.domain_id, collectionId, false);
    remember(source.domain_id, collectionId, false);
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
      const params = new URL(window.location.href).searchParams;
      const requestedDomain = params.get("domain_id");
      const domain = domains.value.find((item) => item.domain_id === requestedDomain) ?? domains.value[0];
      const requestedCollection = params.get("collection_id");
      const collection = collections.value.find((item) => item.collection_id === requestedCollection
        && item.domain_id === domain?.domain_id);
      if (domain) select(domain.domain_id, collection?.collection_id ?? "", false);
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
