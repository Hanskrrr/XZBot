const fs=require('node:fs'),path=require('node:path');
module.exports=function(root){return fs.readFileSync(path.join(root,'ui/index.html'),'utf8')
 .replace(/<script defer src="\/static\/([^"]+)"><\/script>/g,(_,file)=>'<script>'+fs.readFileSync(path.join(root,'ui',file),'utf8')+'</script>')
 .replace(/<link rel="stylesheet" href="\/static\/([^"]+)">/g,(_,file)=>'<style>'+fs.readFileSync(path.join(root,'ui',file),'utf8')+'</style>');};
