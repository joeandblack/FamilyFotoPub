// The sidebar's build identifier: version, branch and short commit of the code this server runs.
export function buildLabel(build){
 if(!build)return '';
 const version=build.version&&build.version!=='unknown'?`v${build.version}`:'';
 const commit=build.commit?build.commit.slice(0,7)+(build.dirty?'+':''):'';
 return [version,commit,build.branch].filter(Boolean).join(' · ');  // Branch last: a long one is cut off, not the commit
}

export function buildTitle(build){
 if(!build)return '';
 return [build.version&&`Version ${build.version}`,build.branch&&`Branch ${build.branch}`,
  build.commit&&`Commit ${build.commit}${build.dirty?' (built with local changes)':''}`].filter(Boolean).join('\n');
}
