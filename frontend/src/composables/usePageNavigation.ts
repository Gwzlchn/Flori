import { ref } from "vue";

type AppView = "about" | "content" | "library" | "system";
type ReaderTab = "artifacts" | "metadata" | "pipeline" | "visuals";

function viewOf(params: URLSearchParams): AppView {
  const value = params.get("view");
  return value === "about" || value === "content" || value === "system" ? value : "library";
}
function tabOf(params: URLSearchParams): ReaderTab {
  const value = params.get("tab");
  return value === "metadata" || value === "pipeline" || value === "visuals" ? value : "artifacts";
}

export function usePageNavigation() {
  const initial = new URL(window.location.href).searchParams;
  const activeView = ref<AppView>(viewOf(initial));
  const readerTab = ref<ReaderTab>(tabOf(initial));
  const selectedTaskKey = ref(initial.get("task_key") ?? "");
  const selectedArtifactId = ref(initial.get("artifact_id") ?? "");
  const selectedVisualId = ref(initial.get("visual_id") ?? "");

  function remember(values: Partial<Record<"artifact_id" | "tab" | "task_key" | "view" | "visual_id", string>>): void {
    const url = new URL(window.location.href);
    for (const [name, value] of Object.entries(values)) {
      if (value) url.searchParams.set(name, value);
      else url.searchParams.delete(name);
    }
    history.replaceState(null, "", url);
  }
  function setView(value: AppView): void { activeView.value = value; remember({ view: value }); }
  function setTab(value: ReaderTab): void { readerTab.value = value; remember({ tab: value }); }
  function setTask(value: string): void { selectedTaskKey.value = value; remember({ task_key: value }); }
  function setArtifact(value: string): void { selectedArtifactId.value = value; remember({ artifact_id: value }); }
  function setVisual(value: string): void { selectedVisualId.value = value; remember({ visual_id: value }); }
  function clearReader(): void {
    readerTab.value = "artifacts";
    selectedTaskKey.value = "";
    selectedArtifactId.value = "";
    selectedVisualId.value = "";
    remember({ artifact_id: "", tab: "artifacts", task_key: "", visual_id: "" });
  }
  function restore(): void {
    const params = new URL(window.location.href).searchParams;
    activeView.value = viewOf(params);
    readerTab.value = tabOf(params);
    selectedTaskKey.value = params.get("task_key") ?? "";
    selectedArtifactId.value = params.get("artifact_id") ?? "";
    selectedVisualId.value = params.get("visual_id") ?? "";
  }

  return {
    activeView, readerTab, selectedTaskKey, selectedArtifactId, selectedVisualId,
    setView, setTab, setTask, setArtifact, setVisual, clearReader, restore,
  };
}
