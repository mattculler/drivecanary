$(document).ready(function() {
  // Prefer the model_family, if it exists
  blkdevs.forEach(function(dev) {
    var model = dev.model;
    if (dev.model_family) {
      model = dev.model_family;
    }
    dev.nice_model = model;
  });

  $("#grid").jsGrid({
    width: "100%",
    height: "auto",

    inserting: false,
    editing: false,
    sorting: true,
    //paging: true,

    data: blkdevs,

    fields: [
      { name: "host", type: "text", width: "50", title: "Host" },
      { name: "kern_name", type: "text", width: "40", title: "Node" },
      { name: "size_bytes", type: "capacity", width: "50", title: "Size" },
      { name: "is_spinning_rust", type: "disktype", width: "50", title: "Disk Type" },
      { name: "nice_model", type: "text", width: "160", title: "Model" },
      { name: "serial", type: "text" },
      { name: "first_seen", type: "date", title: "First Seen" }
    ],

    rowClick: function(wrapt) {
      // Show evey field that is not shown in the main grid
      var propsToRemove = _.pluck(this.fields, "name");
      var fields = _.omit(wrapt.item, propsToRemove);

      var popup = new tingle.modal();
      var content = $("<table>");
      for (var key in fields) {
        content.append("<tr>").append([
          $("<td>" + key + "</td>"),
          $("<td>" + fields[key] + "</td>")
        ]);
      }
      popup.setContent(content[0]);
      popup.open();
    }
  }); 
});
