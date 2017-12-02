$(document).ready(function() {
  // Prefer the model_family, if it exists
  blkdevs.forEach(function(dev) {
    var model = dev.model;
    if (dev.model_family) {
      model = dev.model_family;
    }
    dev.nice_model = model;
  });

  // Docs: http://js-grid.com/docs/
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
      // Got icons here, loads more good ones: https://icons8.com/
      { name: "is_spinning_rust", type: "disktype", width: "50", title: "Disk Type" },
      { name: "nice_model", type: "text", width: "160", title: "Model" },
      { name: "serial", type: "text" },
      { name: "first_seen", type: "date", title: "First Seen" }
    ],

    rowClick: function(wrapt) {
      // Build the heading and outer wrapper
      var content = $("<div>");
      content.append($("<h1>", {
        html: wrapt.item.host + ": " + wrapt.item.kern_name
      }));
      content.append($("<h3>", {
        html: wrapt.item.nice_model
      }));

      // Show evey field that is not shown in the main grid
      var propsToRemove = _.pluck(this.fields, "name");
      var fields = _.omit(wrapt.item, propsToRemove);

      var popup = new tingle.modal();
      var table = $("<table>");
      _.each(_.sortBy(_.keys(fields)), function(key) {
        table.append("<tr>").append([
          $("<td>" + key + "</td>"),
          $("<td>" + fields[key] + "</td>")
        ]);
      });
      content.append(table);
      popup.setContent(content[0]);
      popup.open();
    }
  }); 
});
