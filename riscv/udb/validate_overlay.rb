# frozen_string_literal: true

# Validates the Xglasscol overlay with UDB's own library, for a named config.
# UDB's `./do test:schema` and `./do test:inst_encodings` are hard-wired to the
# unconfigured "_" architecture, which does not include custom overlays, so this
# script runs the same library calls on the glasschip_rv64 config:
#   1. schema validation of every resolved arch file (Architecture#validate)
#   2. the instruction-encoding conflict check from Rakefile test:inst_encodings
#   3. IDL type checking of every instruction and function (ConfiguredArchitecture#type_check)
# Run from the UDB root:  bin/mise exec -- bundle exec ruby <this file> [CFG]
require "set"
require "udb/resolver"

root = Pathname.new(Dir.pwd)
cfg = ARGV[0] || "glasschip_rv64"
resolver = Udb::Resolver.new(root)
cfg_arch = resolver.cfg_arch_for(cfg)

puts "[1/3] schema validation for #{cfg}"
cfg_arch.validate(resolver, show_progress: false)
puts "PASS schema"

puts "[2/3] encoding conflicts for #{cfg}"
insts = cfg_arch.instructions
xcol = insts.find { |i| i.name == "xcol.sdf" }
raise "xcol.sdf not found in #{cfg}" if xcol.nil?
puts "xcol.sdf encoding RV64: #{xcol.encoding(64).format}"
failed = false
names = Set.new
insts.each do |i|
  if names.include?(i.name)
    warn "duplicate instruction name #{i.name}"
    failed = true
  end
  names.add(i.name)
end
checked = 0
insts.each do |other|
  next if other == xcol
  next unless other.defined_in_base?(64)
  checked += 1
  if xcol.bad_encoding_conflict?(64, other)
    warn "conflict: xcol.sdf vs #{other.name} (#{other.encoding(64).format})"
    failed = true
  end
end
raise "encoding check FAILED" if failed
puts "PASS encodings (xcol.sdf against #{checked} RV64 instructions in #{cfg})"

puts "[3/3] IDL type check for #{cfg}"
cfg_arch.type_check
puts "PASS IDL type check"
