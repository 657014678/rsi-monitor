// 全局配置
const CACHE_EXPIRE = 10 * 60 * 1000; // 10分钟缓存
const cacheMap = new Map();
const chartInstances = {};

// CDN 换成国内快的
window.CDN = "https://cdn.jsdelivr.net/npm";

// ---------- 缓存封装 ----------
function getCache(key) {
  const item = cacheMap.get(key);
  if (!item) return null;
  if (Date.now() - item.time > CACHE_EXPIRE) {
    cacheMap.delete(key);
    return null;
  }
  return item.data;
}
function setCache(key, data) {
  cacheMap.set(key, { data, time: Date.now() });
}

// ---------- 无刷新路由 ----------
const routes = {
  "/": "index.html",
  "/index": "index.html",
  "/fund": "fund.html",
  "/kline": "kline.html",
  "/rsi": "rsi.html"
  // 你有几个页面就补几个
};

function router() {
  const hash = location.hash.slice(1) || "/";
  const page = routes[hash] || "index.html";
  loadPage(page);
}

async function loadPage(page) {
  const res = await fetch(page);
  const html = await res.text();
  const main = document.getElementById("main-content");
  if (main) main.innerHTML = html;
  // 每个页面执行自己的 init
  if (window[page.replace(".html", "") + "Init"]) {
    window[page.replace(".html", "") + "Init"]();
  }
}

// ---------- 图表复用 ----------
function getChart(domId) {
  if (chartInstances[domId]) return chartInstances[domId];
  const dom = document.getElementById(domId);
  const chart = echarts.init(dom);
  chartInstances[domId] = chart;
  return chart;
}

// 路由监听
window.addEventListener("hashchange", router);
window.addEventListener("load", router);