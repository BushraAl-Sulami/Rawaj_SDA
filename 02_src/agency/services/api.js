export async function api(path, options = {}) {
  const response = await fetch(path.startsWith('/api/') ? path : '/api/agency' + path, {
    cache:'no-store', ...options, headers:{'Content-Type':'application/json', ...options.headers}
  });
  const data = await response.json().catch(()=>({}));
  if (!response.ok) {
    const detail = data.detail;
    throw new Error(typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(x=>x.msg).join('. ') : `Request failed (${response.status}). Please try again.`);
  }
  return data;
}
export const post = (path, body={}) => api(path,{method:'POST',body:JSON.stringify(body)});
