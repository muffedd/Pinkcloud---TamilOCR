/* Pink Cloud data access.
   USE_MOCK = true  -> page JSON comes from the local schema/doc_demo.json.
   USE_MOCK = false -> the same getPage(n) calls the live API instead.
   UI code must not change when this flag flips. */

const USE_MOCK = true;

async function getPage(n) {
  if (USE_MOCK) {
    const res = await fetch("./schema/doc_demo.json", { cache: "no-store" });
    if (!res.ok) {
      throw new Error("mock page " + n + " failed to load (" + res.status + ")");
    }
    return res.json();
  }
  const res = await fetch("/api/pages/" + encodeURIComponent(n));
  if (!res.ok) {
    throw new Error("page " + n + " failed to load (" + res.status + ")");
  }
  return res.json();
}

window.PC_API = { USE_MOCK, getPage };
