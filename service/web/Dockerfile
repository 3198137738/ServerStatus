FROM nginx:alpine

LABEL maintainer="lidalao"
LABEL description="Static dashboard frontend for ServerStatus"

# 前端静态资源打包进镜像(json/stats.json 运行时由 ./json 挂载提供)
COPY . /usr/share/nginx/html

# 覆盖默认 server 块: 给 html/css/js 加 no-cache 头
# (nginx.conf 不能进 .dockerignore, 否则这里 COPY 不到; 改成事后从站点根目录删)
COPY nginx.conf /etc/nginx/conf.d/default.conf
RUN rm -f /usr/share/nginx/html/nginx.conf && nginx -t

# 静态资源地址带内容哈希(js/app.js?v=xxxx): 反代(如 NPM 的 Cache Assets)/CDN 会无视
# no-cache 按扩展名缓存 js/css, 只有地址变了才能保证拿到新版; index.html 本身不被这类缓存
RUN cd /usr/share/nginx/html \
 && jsv=$(md5sum js/app.js | cut -c1-10) \
 && cssv=$(md5sum css/app.css | cut -c1-10) \
 && sed -i "s#js/app\.js\"#js/app.js?v=${jsv}\"#; s#css/app\.css\"#css/app.css?v=${cssv}\"#" index.html \
 && grep -q "app.js?v=${jsv}" index.html && grep -q "app.css?v=${cssv}" index.html
