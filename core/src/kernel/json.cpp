#include "glasschip/json.hpp"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <stdexcept>

#include "glasschip/kernel.hpp"

namespace gc {

const Json* Json::get(const std::string& key) const {
  if (t != Obj) return nullptr;
  for (const auto& kv : o)
    if (kv.first == key) return &kv.second;
  return nullptr;
}

Json& Json::set(const std::string& key, Json v) {
  if (t == Null) t = Obj;
  for (auto& kv : o)
    if (kv.first == key) { kv.second = std::move(v); return kv.second; }
  o.emplace_back(key, std::move(v));
  return o.back().second;
}

double Json::as_num(double def) const {
  if (t == Num) return n;
  if (t == Bool) return b ? 1.0 : 0.0;
  if (t == Str) {
    char* end = nullptr;
    double v = std::strtod(s.c_str(), &end);
    if (end && end != s.c_str()) return v;
  }
  return def;
}

double Json::num(const std::string& key, double def) const {
  const Json* j = get(key);
  return j ? j->as_num(def) : def;
}

std::string Json::str(const std::string& key, const std::string& def) const {
  const Json* j = get(key);
  if (!j) return def;
  if (j->t == Str) return j->s;
  return def;
}

bool Json::flag(const std::string& key, bool def) const {
  const Json* j = get(key);
  if (!j) return def;
  if (j->t == Bool) return j->b;
  if (j->t == Num) return j->n != 0;
  return def;
}

namespace {
struct P {
  const std::string& s;
  size_t i = 0;
  void ws() { while (i < s.size() && (s[i] == ' ' || s[i] == '\n' || s[i] == '\t' || s[i] == '\r')) ++i; }
  [[noreturn]] void fail(const char* m) { throw std::runtime_error(std::string("json: ") + m + " at " + std::to_string(i)); }
  Json value() {
    ws();
    if (i >= s.size()) fail("eof");
    char c = s[i];
    if (c == '{') {
      ++i;
      Json j = Json::object();
      ws();
      if (s[i] == '}') { ++i; return j; }
      for (;;) {
        ws();
        std::string k = str();
        ws();
        if (s[i] != ':') fail("expected :");
        ++i;
        j.o.emplace_back(k, value());
        ws();
        if (s[i] == ',') { ++i; continue; }
        if (s[i] == '}') { ++i; return j; }
        fail("expected , or }");
      }
    }
    if (c == '[') {
      ++i;
      Json j = Json::array();
      ws();
      if (s[i] == ']') { ++i; return j; }
      for (;;) {
        j.a.push_back(value());
        ws();
        if (s[i] == ',') { ++i; continue; }
        if (s[i] == ']') { ++i; return j; }
        fail("expected , or ]");
      }
    }
    if (c == '"') return Json::string(str());
    if (s.compare(i, 4, "true") == 0) { i += 4; return Json::boolean(true); }
    if (s.compare(i, 5, "false") == 0) { i += 5; return Json::boolean(false); }
    if (s.compare(i, 4, "null") == 0) { i += 4; return Json{}; }
    char* end = nullptr;
    double v = std::strtod(s.c_str() + i, &end);
    if (end == s.c_str() + i) fail("bad value");
    i = static_cast<size_t>(end - s.c_str());
    return Json::number(v);
  }
  std::string str() {
    if (s[i] != '"') fail("expected string");
    ++i;
    std::string out;
    while (i < s.size() && s[i] != '"') {
      if (s[i] == '\\') {
        ++i;
        char e = s[i];
        if (e == 'n') out += '\n';
        else if (e == 't') out += '\t';
        else if (e == 'u') { out += '?'; i += 4; }
        else out += e;
        ++i;
      } else {
        out += s[i++];
      }
    }
    ++i;
    return out;
  }
};

void dump(const Json& j, std::string& out) {
  switch (j.t) {
    case Json::Null: out += "null"; break;
    case Json::Bool: out += j.b ? "true" : "false"; break;
    case Json::Num: {
      char buf[64];
      if (std::fabs(j.n) < 9e15 && j.n == std::floor(j.n)) std::snprintf(buf, sizeof buf, "%lld", static_cast<long long>(j.n));
      else std::snprintf(buf, sizeof buf, "%.17g", j.n);
      out += buf;
      break;
    }
    case Json::Str:
      out += '"';
      for (char c : j.s) {
        if (c == '"' || c == '\\') { out += '\\'; out += c; }
        else if (c == '\n') out += "\\n";
        else out += c;
      }
      out += '"';
      break;
    case Json::Arr:
      out += '[';
      for (size_t k = 0; k < j.a.size(); ++k) { if (k) out += ','; dump(j.a[k], out); }
      out += ']';
      break;
    case Json::Obj:
      out += '{';
      for (size_t k = 0; k < j.o.size(); ++k) {
        if (k) out += ',';
        dump(Json::string(j.o[k].first), out);
        out += ':';
        dump(j.o[k].second, out);
      }
      out += '}';
      break;
  }
}
}  // namespace

Json parse_json(const std::string& text) {
  P p{text};
  return p.value();
}

std::string dump_json(const Json& j) {
  std::string out;
  dump(j, out);
  return out;
}

std::string StreamHash::hex() const {
  char buf[48];
  std::snprintf(buf, sizeof buf, "%016llx%016llx", static_cast<unsigned long long>(h1), static_cast<unsigned long long>(h2));
  return buf;
}

}  // namespace gc
