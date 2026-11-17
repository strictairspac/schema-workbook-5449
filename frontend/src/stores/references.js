import {referencesApi} from '../api/references.js';
export const referencesStore={
  items:[],
  async refresh(){this.items=await referencesApi.list();return this.items;},
  async effective(relationId){return referencesApi.effective(relationId);},
};
