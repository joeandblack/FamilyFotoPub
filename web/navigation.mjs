export const navigationKey='familyfoto.navigation';
export function viewForRole(view,role){
  if(role!=='admin'&&['imports','import-overview','settings','storage'].includes(view))return 'all';
  return view;
}
export function normalizeNavigation(value){
  const state=value&&typeof value==='object'?{...value}:{};
    if(state.view==='storage')state.view='settings';
  return {view:['all','image','video','trash','imports','uploads','import-overview','settings','albums','faces','people','people-preview'].includes(state.view)?state.view:'all',
    page:Number.isSafeInteger(state.page)&&state.page>0?state.page:1,
    year:typeof state.year==='string'&&/^(all|unknown|\d{4})$/.test(state.year)?state.year:'all',
    import_job:typeof state.import_job==='string'&&/^[1-9]\d*$/.test(state.import_job)&&Number.isSafeInteger(Number(state.import_job))?state.import_job:'all',
    search:typeof state.search==='string'?state.search:''};
}
export function readNavigation(storage){
  try{return normalizeNavigation(JSON.parse(storage.getItem(navigationKey)));}catch{return normalizeNavigation(null);}
}
export function saveNavigation(storage,state){
  try{storage.setItem(navigationKey,JSON.stringify(normalizeNavigation(state)));}catch{}
}

export function importNavigation(id){return normalizeNavigation({view:"all",page:1,year:"all",search:"",import_job:String(id)});}
