import {api} from './client.js';
export const referencesApi={
  list:()=>api('/references'),
  create:data=>api('/references','POST',data),
  updateMapping:(id,mapping)=>api(`/references/${id}/mapping`,'PUT',{mapping}),
  retry:id=>api(`/references/${id}/retry`,'POST'),
  effective:relationId=>api(`/relations/${relationId}/effective`),
  propagate:relationId=>api(`/relations/${relationId}/propagate`,'POST'),
};
